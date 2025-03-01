import docker
import tempfile
import os
import inspect
import json 
#import numpy

import pyatmos

#_________________________________________________________________________
def print_list(li):
    for e in li:
        print(e.replace('\n',''))

#_________________________________________________________________________
def format_datetime(unix_timestamp):
    '''
    Convert unix timestamp to human readable format
    Should automatically be in the timezone of the host machine
    '''
    import datetime
    return datetime.datetime.fromtimestamp(
        int(unix_timestamp)
    ).strftime('%Y-%m-%d %H:%M:%S')


#_________________________________________________________________________
class Simulation():
    def __init__(self, 
            docker_image='registry.gitlab.com/frontierdevelopmentlab/astrobiology/pyatmos', 
            code_path=None,
            DEBUG=False, 
            atmos_directory = '/code/atmos'):
        '''
        docker_image: string (optional). If specified, pyatmos will communicate with a docker image, otherwise use the code_path 
        code_path: string (optional). If specified, pyatmos will communicate with a local version of atmos. The string is the path to the atmos directory 
        DEBUG: bool, if set to true, extra debug messages are printed
        '''

        # get input arguments
        self._docker_image    = docker_image
        self._code_path       = code_path
        self._debug           = DEBUG
        self._atmos_directory = atmos_directory

        # check if properly initialsed
        if (self._docker_image is not None) and (self._code_path is not None):
            raise RuntimeError( 'ERROR: specify _either_ a docker images, or a local code path to ATMOS, but not both')
        if (self._docker_image is None) and (self._code_path is None):
            raise RuntimeError('ERROR: you must specify _either_ a docker image, or a local code path to ATMOS')


        # initialize docker if need be 
        if self._docker_image is not None:
            self._initialize_docker()
        else:
            self._atmos_directory = self._code_path 

        # initialize other runtime variables
        self._save_logfiles = False
        self._container = None
        self._run_iteration_call = None

        # metadata for runtime 
        self._start_time         = 0
        self._run_time_start     = 0
        self._run_time_end       = 0
        self._photochem_duration = 0
        self._clima_duration     = 0
        self._initialize_time    = pyatmos.util.UTC_now()

        # metadata for run parameters
        self._species_concentrations = None
        self._max_photochem_iterations = None
        self._max_clima_steps = None

        # other run metadata
        self._n_photochem_iterations = None 
        self._n_clima_iterations = None
        print('Initialization complete: '+format_datetime(self._initialize_time))

    #_________________________________________________________________________
    def _initialize_docker(self):
        print('Initializing Docker...')
        self._docker_client = docker.from_env()
        print('Pulling latest image... {}'.format(self._docker_image))
        self._docker_client.images.pull(self._docker_image)
        self._container = None



    #_________________________________________________________________________
    def start(self):
        self._start_time = pyatmos.util.UTC_now()
        if self._docker_image is not None:
            print('Starting Docker container...')
            self._container = self._docker_client.containers.run(self._docker_image, detach=True, tty=True)
            print("Container '{0}' running at {1}.".format(self._container.name, format_datetime(self._start_time) ))
        else:
            print('pyatmos is ready to go! ')

    #_________________________________________________________________________
    @staticmethod
    def split_dictionary(input_dict, species='N2'):
        '''
        Splits the input dictionary into two by taking out the 'species' element from the input dictionary and putting that into a new one
        Args:
            input_dict: dictionary
            species: key, a key that is inside the input_dictionary
        Returns
            input_dict: dictionary, a dictionary with the "species" element removed
            separated_dict: dictionary, a dictionary containing the removed element {species : value}
        '''

        try:
            value = input_dict.pop(species)
            separated_dict = {species : value}
        except KeyError:
            separated_dict = {}
        return input_dict, separated_dict

    #_________________________________________________________________________
    def run_distance_modification(self, 
            flux_scaling=1.0, 
            max_clima_steps=400, 
            save_logfiles = False,
            output_directory=None):
        """Test function to modify the earth--sun distance
        meant to be run iteratively 
        Args:
            flux_scaling: float, the fraction of the solar radiance relative to earth. Value of 1.0 corresponds to earth
            Distance scales as 1/a^2 (a is semi-major axis) 
        """

        # parse input arguments
        self._save_logfiles = save_logfiles

        # Make sure output directory set 
        if output_directory is None:
            raise RuntimeError('Error, you must set the output_directory')


        # make sure output directory exists
        cmd = 'mkdir -p {0}'.format(output_directory)
        print("Will create the output directory if it does not exist:")
        print(cmd)
        os.system(cmd)

        # modify the clima input file with the flux scaling  
        # and make sure ICOUPLE=   0 (since we're probably not running in coupled mode?) TODO, consider if this is the case? 
        self.debug('reading file {0}'.format(self._atmos_directory+'/CLIMA/IO/input_clima.dat'))

        clima_input = self._read_container_file(self._atmos_directory+'/CLIMA/IO/input_clima.dat') # clima_input: file containing strings of input_clima.dat 
        new_clima_file_name = tempfile.NamedTemporaryFile().name
        new_clima_file = open(new_clima_file_name, 'w')
        for line in clima_input:
            if 'SOLCON=  ' in line:
                line = 'SOLCON=    {0}\n'.format(flux_scaling)
            if 'ICOUPLE=   ' in line:
                line = 'ICOUPLE=   0\n'
            new_clima_file.write(line)
        new_clima_file.close()
        self._write_container_file(new_clima_file_name, self._atmos_directory+'/CLIMA/IO/input_clima.dat')

        # run clima
        clima_converged = self._run_clima(max_clima_steps, output_directory, methane_concentration = 0)
        
        # parse the output of photochem and clima (writes output as pandas csv file) 
        pyatmos.parser.parse_clima(input_file = output_directory+'/clima_allout.tab',
                                    output_directory = output_directory,
                                    debug=self._debug )

        return clima_converged


    #_________________________________________________________________________
    def run(self, 
            species_concentrations={}, 
            species_fluxes={},
            max_photochem_iterations=10000, 
            max_clima_steps=500, 
            previous_photochem_solution = None,
            previous_clima_solution = None, 
            output_directory='/Users/Will/Documents/FDL/results',
            run_iteration_call = None,
            save_logfiles = False
            ):
        '''
        Configures and runs ATMOS, then collects the output.  
        - Modifes species file with custom concentrations (supplied via species_concentrations) 
        - Runs the photochemical model and checks for convergence in max_photochem_iterations steps 
        - If converged, then runs the clima model. First modifies the clima input file with max_clima_steps   
        - copies the results files to output_directory 

        Args: 
            species_concentrations: dictionary of species and concentrations to change them to, formatted as 
                                    { 'species name' : concentration (float) }
                                    concentration should be fractional (not a percentage) 
                                    Note that the species must be different from those specified in species_fluxes
            species_fluxes: dictionary of species and fluxes. These values will overwrite the defaults in species.dat
                                    The dictionary is formatted as:
                                    { 'species name' : flux (float) }
                                    fluxes should be in molecues/s/cm^2   
                                    Note that the species must be different from those specified in species_concentrations
            max_photochem_iterations: int, maximum number of iterations allowed by photochem to test for convergence  
            max_clima_steps: int, number of steps taken by clima (default 400) 
            previous_photochem_solution: string, path to the previous solution for photochem (the "out.dist" file, which will become the "in.dist" file)
            previous_clima_solution: string, path to the previous clima solution (the "TempOut.dat" file, which will become the "TempIn.dat" file)
            output_directory: string, path to the directory to store outputs (on your own filesystem!!) 
            save_logfiles: bool, if True, the output of clima and photochem will be saved to a logfile and written to the output directory
        '''

        # check the input species dictionaries 
        concentration_keys = species_concentrations.keys()
        flux_keys          = species_fluxes.keys()
        overlapping_species = set(flux_keys).intersection( set( concentration_keys) )
        if (overlapping_species):
            print ("ERROR: cannot modify the flux AND the concentration for the same species. Problem for species: {0}".format(overlapping_species)) 
            raise RuntimeError
        if flux_keys and concentration_keys:
            print('Will attempt to modify species file with fluxes {0} and concentrations {1}'.format(species_fluxes, species_concentrations))

        # make the output directory
        os.system('mkdir -p '+output_directory)

        # set metadata
        self._run_iteration_call = run_iteration_call
        self._save_logfiles = save_logfiles 
        self._species_concentrations = species_concentrations 
        self._max_photochem_iterations = max_photochem_iterations
        self._max_clima_steps = max_clima_steps 
        self._run_time_start = pyatmos.util.UTC_now() 


        # make sure we're in the right directory
        self._generic_run('cd '+self._atmos_directory) 


        # run the photochemical model 
        photochem_converged = self._run_photochem(species_concentrations, species_fluxes, max_photochem_iterations, output_directory, previous_photochem_solution)

        # if photochem didn't converge, exit 
        if photochem_converged != 'success': 
            return photochem_converged
        else:
            print('photochem converged')

        # run clima  
        if 'CH4' in species_concentrations.keys():
            methane_concentration = species_concentrations['CH4'] 
        else: 
            methane_concentration = 1.80E-06 
        clima_converged = self._run_clima(max_clima_steps, output_directory, methane_concentration, previous_clima_solution)

        # if clima didn't converge, exit
        if not clima_converged:
            self._run_time_end = pyatmos.util.UTC_now()
            return 'clima_error'
        else:
            print('clima converged')


        # parse the output of clima (writes output as pandas csv file) 
        pyatmos.parser.parse_clima(input_file = output_directory+'/clima_allout.tab',
                                    output_directory = output_directory,
                                    debug=self._debug )

        # parse the output of photochem (writes output as pandas csv file) 
        pyatmos.parser.parse_photochem(input_file = output_directory+'/out.out',
                                    output_directory = output_directory,
                                    debug=self._debug )

        #########################################
        # Add *basic* plots to the output directory 
        # Hard coded here, one should do this offline 
        #########################################

        # get the clima dataframe
        try:
            import pandas as pd 
            # plotting for clima
            self.debug('read pandas dataframe {0}'.format(output_directory+'/parsed_clima_final.csv'))
            clima_df = pd.read_csv(output_directory+'/parsed_clima_final.csv')
            self.debug('Creating plot {0}'.format(output_directory+'/pressure_altitide.pdf'))
            pyatmos.util.plot_scatter(clima_df, xvariable='P', xlabel='Pressure [bar]', yvariable='ALT', ylabel='Altitide [km]', save_name = output_directory+'/pressure_altitide.pdf')   
            self.debug('Creating plot {0}'.format(output_directory+'/pressure_temperature.pdf'))
            pyatmos.util.plot_scatter(clima_df, xvariable='T', xlabel='Temperature [K]', yvariable='ALT', ylabel='Altitide [km]', save_name = output_directory+'/pressure_temperature.pdf')   
        except:
            print('Exception occoured during clima plotting, not handeled')
            pass

        try:
            # get photochem dataframes 
            self.debug('read pandas dataframe {0}'.format(output_directory+'/parsed_photochem_mixing_ratios.csv'))
            photo_mixing_df = pd.read_csv(output_directory+'/parsed_photochem_mixing_ratios.csv')
            self.debug('read pandas dataframe {0}'.format(output_directory+'/parsed_photochem_fluxes.csv'))
            photo_flux_df   = pd.read_csv(output_directory+'/parsed_photochem_fluxes.csv')
            # convert cm to km 
            photo_mixing_df['Z']  = photo_mixing_df['Z']/1e5
            photo_flux_df['Z']    = photo_flux_df['Z']/1e5 
            # make plot
            gases = ['O3', 'H2O', 'CO', 'CH4', 'CO2', 'O2', 'H2'] 
            self.debug('Creating plot {0}'.format(output_directory + '/mixingratio_altitide.pdf'))
            pyatmos.util.plot_multiscatter(photo_mixing_df, xvariables=gases, xlabel='Mixing ratio', yvariable='Z', ylabel='Altitide [km]', save_name = output_directory + '/mixingratio_altitide.pdf')
            self.debug('Creating plot {0}'.format(output_directory + '/flux_altitide.pdf'))
            pyatmos.util.plot_multiscatter(photo_flux_df, xvariables=gases, xlabel='Flux [molecules s$^{-1}$ cm$^{-2}$]', yvariable='Z', ylabel='Altitide [km]', save_name = output_directory + '/flux_altitide.pdf')
        except:
            print('Exception occoured during photochem plotting, not handeled')
            pass 

        self._run_time_end = pyatmos.util.UTC_now()
        print('Running finished.')

        return 'success' 

    #_________________________________________________________________________
    def write_metadata(self, output_path, extra_information = {}):
        metadata = self.get_metadata()

        # merge dictionaries 
        if extra_information:
            metadata = { **metadata, **extra_information }  
        with open(output_path, 'w') as fp:
            json.dump(metadata, fp, sort_keys=True, indent=4)


    #_________________________________________________________________________
    def get_metadata(self):

        return {
                'atmos_start_time' : self._run_time_start,
                'photochem_duration' : self._photochem_duration,
                'photochem_iterations' : self._n_photochem_iterations,  
                'clima_duration' : self._clima_duration,
                #'clima_iterations' : self._n_clima_iterations, # TO DO, clima iterations not set   
                'atmos_run_duration' : self._run_time_end - self._run_time_start,
                'input_max_clima_iterations' : self._max_clima_steps,
                'input_max_photochem_iterations' : self._max_photochem_iterations,
                'input_species_concentrations' : self._species_concentrations,
                'write_logfiles' : self._save_logfiles,
                'run_iteration_call' : self._run_iteration_call
                }

    #_________________________________________________________________________
    def _run_photochem(self, species_concentrations, species_fluxes, max_photochem_iterations, output_directory, previous_photochem_solution):
        '''
        Function to actually run the photochemical model, copies the results once finished 
        '''

        ################################
        # modify species file, changes the concentrations inside species.dat as specified by species_concentrations
        ################################
        #self._modify_atmospheric_species(self._atmos_directory+'/PHOTOCHEM/INPUTFILES/species.dat', species_concentrations, species_fluxes) 
        self._modify_atmospheric_species(self._atmos_directory+'/PHOTOCHEM/INPUTFILES/TEMPLATES/ModernEarth/species.dat', species_concentrations, species_fluxes) 
        print('Modified species file with concentrations: {0}'.format(species_concentrations) )
        print('Modified species file with fluxes: {0}'.format(species_fluxes) )

        
        # put in the new in.dist file (can be from previous run of photochem)
        if previous_photochem_solution:
            self._write_container_file(previous_photochem_solution, self._atmos_directory+'/PHOTOCHEM/in.dist')


        
        ################################
        # Run photochem 
        ################################
        self._photochem_duration = pyatmos.util.UTC_now()
        print('About to run photochem ... ')
        if self._docker_image is not None:
            self._container.exec_run('./Photo.run')
        else:
            if self._save_logfiles:
                self._generic_run('cd {0} && ./Photo.run > {1}/Photo_log.txt 2>&1'.format(self._atmos_directory, output_directory))
                #self._generic_run('cd {0} && ./Photo.run 2>&1 | tee {1}/Photo_log.txt'.format(self._atmos_directory, output_directory))
            else:
                self._generic_run('cd {0} && ./Photo.run'.format(self._atmos_directory))
        self._photochem_duration = pyatmos.util.UTC_now() - self._photochem_duration 

        # check for convergence of photochem   
        try:
            [photochem_converged, n_photochem_iterations] = self._check_photochem_convergence(max_photochem_iterations)
        except IndexError:
            return 'photochem_error'

        self._n_photochem_iterations = n_photochem_iterations 
        if not photochem_converged:
            return 'photochem_nonconverged'

        print('photochem finished after {0} iterations and took {1} seconds'.format(n_photochem_iterations, self._photochem_duration))


        ################################
        # copy photochem results
        ################################

        print('Copying photochem results to {0}'.format(output_directory))
        self._copy_container_file(self._atmos_directory+'/PHOTOCHEM/OUTPUT/out.out', output_directory)
        self._copy_container_file(self._atmos_directory+'/PHOTOCHEM/OUTPUT/out.dist', output_directory) 
        self._copy_container_file(self._atmos_directory+'/PHOTOCHEM/INPUTFILES/species.dat', output_directory)
        # this command may not work if photochem has not been run before? 
        self._copy_container_file(self._atmos_directory+'/PHOTOCHEM/in.dist', output_directory) # save the "in.dist" file that _was_ used for the next run 

        # Internal copy of photochem results inside the docker image, ready for the next run of photochem 
        self._generic_run("cp  {0}/PHOTOCHEM/OUTPUT/out.dist {0}/PHOTOCHEM/in.dist".format(self._atmos_directory))

        print('Run photochem finished')

        return 'success' 

    #_________________________________________________________________________
    def _run_clima(self, max_clima_steps, output_directory, methane_concentration, previous_clima_solution=None):


        # Make sure the output directory exists
        command = "mkdir -p {0}".format(output_directory)
        print("Will try to make directory "+output_directory)
        os.system(command)

        ################################
        # Deal with clima input to get it ready for running 
        ################################

        # To help with convergence, replace /CLIMA/IO/TempIn.dat with /CLIMA/IO/TempOut.dat
        # Write the new TempIn.dat file (can be from previous run of clima)
        if previous_clima_solution:
            self._write_container_file(previous_clima_solution, self._atmos_directory+'/CLIMA/IO/TempIn.dat')
        else:
            self._generic_run("cp  {0}/CLIMA/IO/TempOut.dat {0}/CLIMA/IO/TempIn.dat".format(self._atmos_directory)) # internal copy (within the docker container, in case clima has been run twice) 

        # Modify CLIMA/IO/TEMPLATES/ModernEarth/input_clima.dat to change NSTEPS parameter, 
        # and also change IMET parameter depending on methane concentration.  
        clima_input = self._read_container_file(self._atmos_directory+'/CLIMA/IO/input_clima.dat') # clima_input: file containing strings of input_clima.dat 
        replacement_clima = [] 
        for line in clima_input:
            if 'NSTEPS=' in line:
                line = 'NSTEPS=    {0}           !step number (200 recommended for coupling)\n'.format(max_clima_steps)
            if 'IMET=' in line and methane_concentration > 1e-4:
                line = 'IMET=      1\n'
            if 'IUP=       1' in line:
                line = 'IUP=       0\n' 
            replacement_clima.append(line)
        tmp_file_name = tempfile.NamedTemporaryFile().name
        tmp_file = open(tmp_file_name, 'w')
        for l in replacement_clima:
            tmp_file.write(l)
        tmp_file.close() # VERY important to close the file!! 
        self._write_container_file(tmp_file_name, self._atmos_directory+'/CLIMA/IO/input_clima.dat')

        # Set "IUP=       0" in /CLIMA/IO/input_clima.dat 
        #self._generic_run("sed -i 's/IUP=       1/IUP=       0/g' {0}/CLIMA/IO/input_clima.dat".format(self._atmos_directory))



        ################################
        # Run clima 
        ################################

        print('running clima with {0} steps ...'.format(max_clima_steps))
        self._clima_duration = pyatmos.util.UTC_now()
        if self._docker_image is not None:
            self._container.exec_run('./Clima.run')
        else:
            if self._save_logfiles:
                self._generic_run('cd {0} && ./Clima.run > {1}/Clima_log.txt 2>&1'.format(self._atmos_directory, output_directory))
                #self._generic_run('cd {0} && ./Clima.run 2>&1 | tee {1}/Clima_log.txt'.format(self._atmos_directory, output_directory))
            else:
                self._generic_run('cd {0} && ./Clima.run'.format(self._atmos_directory))
        self._clima_duration = pyatmos.util.UTC_now() - self._clima_duration 
        print('finished clima after {0} seconds'.format(self._clima_duration))

        # copy clima output files out of docker image  
        self._copy_container_file(self._atmos_directory+'/CLIMA/IO/clima_allout.tab', output_directory)
        self._copy_container_file(self._atmos_directory+'/CLIMA/IO/TempOut.dat', output_directory) # potentially needed for next run
        self._copy_container_file(self._atmos_directory+'/CLIMA/IO/TempIn.dat', output_directory) # keep for debugging purposes 
        self._copy_container_file(self._atmos_directory+'/COUPLE/mixing_ratios.dat', output_directory)  
        self._copy_container_file(self._atmos_directory+'/CLIMA/IO/input_clima.dat', output_directory) 


        # post-process catch clima errors 
        # Read the log file
        if self._save_logfiles:
            clima_logfile_path = output_directory + '/Clima_log.txt'
            with open(clima_logfile_path, 'r') as file:
                for line in file.readlines():
                    if ('Backtrace for this error:' in line) or ('#9  0xffffffffffffffff' in line): # can add more erros to this if they are found 
                        print('Detected clima crash inside logfile: {0}'.format(line))
                        return False

        print('Running clima finished')

        return True 


    
    #_________________________________________________________________________
    @staticmethod
    def get_surface_fluxes(parsed_photochem_file, gas_fluxes):
        '''
        Return the gas flux at the suface from the processed photochem output file
        Args:
            parsed_photochem_file: a processed photochem file with fluxes, in csv format
            gas_fluxes: list of gases to get the flux for
        Returns:
            A dictionary of {'gas' : flux }
        '''
        import pandas as pd
        df = pd.read_csv(parsed_photochem_file)
        fluxes = df[df['Z'] == 0]
        surface_fluxes = {}
        for gas in gas_fluxes:
            surface_fluxes['flux_'+gas] = float(fluxes[gas]) # save only the float, that's all we need!
        return surface_fluxes



    #_________________________________________________________________________
    @staticmethod
    def get_surface_temperature(parsed_clima_file):
        '''
        Return the surface temperature in Kelvin [K] from the processed clima output 
        Args:
            parsed_clima_file: a processed clima file in csv format
        Returns:
            temperature at the surface in Kelvin
        '''
        import pandas as pd 
        df = pd.read_csv(parsed_clima_file)
        temp = df[df['ALT'] ==0]['T']
        return float(temp) 
        

    #_________________________________________________________________________
    @staticmethod
    def get_surface_pressure(parsed_clima_file):
        '''
        Return the surface pressure in [bar] from the processed clima output
        Args:
            parsed_clima_file: a processed clima file in csv format
        Returns:
            pressure at the surface in bar
        '''
        import pandas as pd
        df = pd.read_csv(parsed_clima_file)
        pressure = df[df['ALT'] ==0]['P']
        return float(pressure)

    #_________________________________________________________________________
    @staticmethod
    def get_final_clima_deviation(parsed_clima_file):
        import pandas as pd
        df = pd.read_csv(parsed_clima_file)
        n_iterations = df['NST'].max()
        new_df = df[df['NST'] == n_iterations]
        DIVFrms = new_df['DIVFrms']
        return float(DIVFrms)


    #_________________________________________________________________________
    def print_run_metadata(self):
        '''
        Prints metadata from the previous call of run 
        '''
        print('Photochem duration {0}'.format(self._photochem_duration))
        print('Clima duration {0}'.format(self._clima_duration))


    #_________________________________________________________________________
    def _modify_atmospheric_species(self, old_species_filename, species_concentrations, species_fluxes):
        '''
        Modify the species file (species_file_name) to find-and-replace the concentrations listed in species_concentrations
        Copies the files out of the docker image, modifies them, and then puts them back 
        Args:
            old_species_filename: string, path to species file inside the docker image
            species_concentrations: dictionary, containing species' concentrations' to modify
            species_fluxes: dictionary, containing species' fluxes to modify 
        '''

        # sort-out the species_concentrations and species_fluxes dictionary to make sure 'N2' if it is present is in a separate dictionary
        ll_concentrations, sl_concentrations = self.split_dictionary(species_concentrations, 'N2')
        ll_fluxes, sl_fluxes                 = self.split_dictionary(species_fluxes, 'N2')
        

        # copy existing species file
        tmp_input_file_name = tempfile.NamedTemporaryFile().name
        self._copy_container_file( old_species_filename, tmp_input_file_name )
        
        # parse existing species file
        longlived_df, other_df = pyatmos.modify_species_file.speciesfile_to_df(tmp_input_file_name)

        # modify the species dataframes with the new concentrations and fluxes 
        longlived_df = pyatmos.modify_species_file.modify_flux(longlived_df, ll_fluxes)
        longlived_df = pyatmos.modify_species_file.modify_concentrations(longlived_df, ll_concentrations)

        other_df = pyatmos.modify_species_file.modify_flux(other_df, sl_fluxes)
        other_df = pyatmos.modify_species_file.modify_concentrations(other_df, sl_concentrations)

        # write the new species file 
        new_species_filename = tempfile.NamedTemporaryFile().name
        ofile = open(new_species_filename, 'w')
        ofile.write( pyatmos.modify_species_file.species_header() ) 
        ofile.write( pyatmos.modify_species_file.write_species_longlived( longlived_df ))
        ofile.write( pyatmos.modify_species_file.write_species_other( other_df ))
        ofile.close()

        # Over-write the species file 
        #self._write_container_file(new_species_filename, old_species_filename) 
        self._write_container_file(new_species_filename, self._atmos_directory+'/PHOTOCHEM/INPUTFILES/species.dat' ) 
    

    #_________________________________________________________________________
    def _check_photochem_convergence(self, max_photochem_iterations):
        '''
        Check that photochem has converged, search the output file for N = (number)
        if number < max_photochem_iterations then convergence has been achived 
        Args:
            max_photochem_iterations: an interger with the maximum number of iterations for convergence 
        '''
        #output = self._generic_run("grep 'N =' /code/atmos/PHOTOCHEM/OUTPUT/out.out")
        #print('output\n')
        #print(output)

        # output is a string containing the lines of /PHOTOCHEM/OUTPUT/out.out 
        output = self._read_container_file(self._atmos_directory+'/PHOTOCHEM/OUTPUT/out.out')

        # find last "N = " and "EMAX"
        iterations = []
        for line in output:
            if 'N =' in line and 'EMAX' in line:
                iterations.append(line)
        last_line = iterations[-1]
        last_line = ' '.join(last_line.split()) # merge whitespace 
        number_of_iterations = int(last_line.split()[2])

        if number_of_iterations < max_photochem_iterations:
            return [True, number_of_iterations]
        else:
            return [False, number_of_iterations] 

    #_________________________________________________________________________
    def _write_container_file(self, input_file_name, output_file_name):
        '''
        Copies a file INTO of docker image 
        Args:
            input_file_name: string, path of file on local filesystem
            output_file_name: string, path of file inside docker image
        '''
        if self._docker_image is not None:
            cmd = 'docker cp {0} {1}:{2}'.format(input_file_name, self._container.name, output_file_name)
        else:
            cmd = 'cp {0} {1}'.format(input_file_name, output_file_name)
        self.debug(cmd)
        os.system(cmd)

    #_________________________________________________________________________
    def _copy_container_file(self, input_file_name, output_path):
        '''
        Copies a file OUT of the docker image
        Args:
            input_file_name: string, path of file inside the docker image
            output_path: string, destination path (or directory) of file 
        '''

        if self._docker_image is not None:
            cmd = 'docker cp ' + self._container.name +':'+input_file_name + ' ' + output_path  
        else:
            cmd = 'cp {0} {1}'.format(input_file_name, output_path)
        self.debug(cmd)
        os.system(cmd) 


    #_________________________________________________________________________
    def _read_container_file(self, container_file_name):
        '''
        Copy file out of the container and turn it into python strings 
        '''
        tmp_file_name = tempfile.NamedTemporaryFile().name
        self._copy_container_file(container_file_name, tmp_file_name) 
        #cmd = 'docker cp ' + self._container.name + ':' + container_file_name + ' ' + tmp_file_name
        #self.debug(cmd)
        #os.system(cmd)
        return pyatmos.util.strings_file(tmp_file_name)

    #_________________________________________________________________________
    def _generic_run(self, command):
        '''
        Runs command either inside docker or simple os system command
        Args:
            command: string, the command to be executed
        '''
        if self._docker_image is not None:
            self._container.exec_run(command)
        else:
            os.system(command)


        if self._debug: 
            caller_name = inspect.stack()[1][3]
            debug_message = '{0}(): {1}'.format(caller_name, command)
            print(pyatmos.util.printcol(debug_message, 'yellow'))



    #_________________________________________________________________________
    def debug(self, message):
        '''
        Printing of debug messages, includes the name of the function which called it 
        Args:
            message: string, message to be printed  
        '''
        if self._debug: 
            caller_name = inspect.stack()[1][3]
            debug_message = '{0}(): {1}'.format(caller_name,message)
            print(pyatmos.util.printcol(debug_message, 'yellow'))




    '''
    #_________________________________________________________________________
    def _get_container_file(self, container_file_name):
        # Copies a file OUT of the docker image to a temp file, and then returns the string of at file  
        tmp_file_name = tempfile.NamedTemporaryFile().name
        cmd = 'docker cp ' + self._container.name + ':' + container_file_name + ' ' + tmp_file_name
        self.debug(cmd)
        os.system(cmd)
        return pyatmos.util.read_file(tmp_file_name)


    def get_input_clima(self):
        return self._get_container_file(self._atmos_directory+'/CLIMA/IO/input_clima.dat')

    def get_input_photochem(self):
        return self._get_container_file(self._atmos_directory+'/PHOTOCHEM/INPUTFILES/input_photchem.dat')


    '''

    #_________________________________________________________________________
    def __enter__(self):
        return self

    #_________________________________________________________________________
    def __exit__(self, exception_type, exception_value, traceback):
        self.close()

    #_________________________________________________________________________
    def __del__(self):
        self.close()

    #_________________________________________________________________________
    def close(self):
        print('Exiting...')
        if (self._container is not None) and (self._docker_image is not None):
            print('Container {0} killed.'.format(self._container.name))
            self._container.kill()
