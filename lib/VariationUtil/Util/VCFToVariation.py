import uuid
import shutil
import os
import subprocess
import logging
import time
import binascii
import vcf
import gzip
import hashlib
from pprint import pprint as pp

from installed_clients.DataFileUtilClient import DataFileUtil
from installed_clients.WorkspaceClient import Workspace

logging.basicConfig(format='%(created)s %(levelname)s: %(message)s')


def log(message, prefix_newline=False):
    """Logging function, provides a hook to suppress or redirect log messages."""
    print(('\n' if prefix_newline else '') + '{0:.2f}'.format(time.time()) + ': ' + str(message))


def is_gz_file(filepath):
    with open(filepath, 'rb') as test_f:
        return binascii.hexlify(test_f.read(2)) == b'1f8b'


def md5_sum_local_file(fname):
    md5hash = hashlib.md5()
    with open(fname, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5hash.update(chunk)
    return md5hash.hexdigest()


def gzip_file(path):
    gzip_path = path + '.gz'

    with open(path, 'rb') as f_in, gzip.open(gzip_path, 'wb') as f_out:
        f_out.writelines(f_in)

    return gzip_path


class VCFToVariation:
    def __init__(self, config):
        self.scratch = config['scratch']
        self.ws_url = config['workspace-url']
        self.callback_url = os.environ['SDK_CALLBACK_URL']
        self.dfu = DataFileUtil(self.callback_url)
        self.wsc = Workspace(self.ws_url)

    def _parse_vcf_data(self, params):
        vcf_filepath = self._stage_input(params)

        # file is validated by this point, can assume vcf_filepath is valid
        reader = vcf.Reader(open(vcf_filepath, 'r'))

        version = float(reader.metadata['fileformat'][4:6])
        genotypes = reader.samples
        chromosomes = []
        contigs = {}
        totalvars = 0

        for record in reader:
            totalvars += 1
            if record.CHROM not in chromosomes:
                chromosomes.append(record.CHROM)

            if record.CHROM not in contigs.keys():
                passvar = 1 if not record.FILTER else 0

                contigs[record.CHROM] = {
                    'contig_id': record.CHROM,
                    'totalvariants': 1,
                    'passvariants': passvar,
                    'length': int(record.affected_end-record.affected_start),
                }
            else:
                contigs[record.CHROM]['totalvariants'] += 1
                if not record.FILTER:
                    contigs[record.CHROM]['passvariants'] += 1

        vcf_info = {
            'version': version,
            'contigs': contigs,
            'total_variants': totalvars,
            'genotype_ids': genotypes,
            'chromosome_ids': chromosomes,
            'file_ref': vcf_filepath
        }

        return vcf_info

    def _validate_vcf_to_sample(self, vcf_genotypes, sample_ids):
        genos_not_found = []

        vgenotypes = [x.upper().strip() for x in vcf_genotypes]
        sids = [x.upper().strip() for x in sample_ids]

        for geno in vgenotypes:
            if geno not in sids:
                genos_not_found.append(geno)

        if not genos_not_found:
            return True
        else:
            return genos_not_found

    def _chk_if_vcf_ids_in_assembly(self, vcf_chromosomes, assembly_chromosomes):
        chromos_not_in_assembly = []

        pp(assembly_chromosomes)

        for chromo in vcf_chromosomes:
            if chromo not in assembly_chromosomes:
                chromos_not_in_assembly.append(chromo)

        if not chromos_not_in_assembly:
            return True
        else:
            return chromos_not_in_assembly

    def _get_vcf_version(self, vcf_filepath):
        with(gzip.open if is_gz_file(vcf_filepath) else open)(vcf_filepath, 'rt') as vcf:
            line = vcf.readline()
            tokens = line.split('=')

            if not (tokens[0].startswith('##fileformat')):
                log("Invalid VCF.  ##fileformat line in meta is improperly formatted.")
                raise ValueError("Invalid VCF.  ##fileformat line in meta is improperly formatted. "
                                 "Check VCF file specifications: https://samtools.github.io/hts-specs/")

            vcf_version = float(tokens[1][-4:].rstrip())

            return vcf_version

    def validate_vcf(self, params):
        if 'genome_ref' not in params:
            raise ValueError('Genome reference not in input parameters: \n\n'+params)
        if 'vcf_staging_file_path' not in params:
            raise ValueError('VCF staging file path not in input parameters: \n\n' + params)


        vcf_filepath = self._stage_input(params)

        vcf_version = self._get_vcf_version(vcf_filepath)

        # setup directorys for validation output
        validation_output_dir = os.path.join(self.scratch, 'validation_' + str(uuid.uuid4()))
        os.mkdir(validation_output_dir)

        # vcftools (vcf-validator) supports VCF v4.0-4.2
        # https://github.com/vcftools/vcftools

        # EBIvariation/vcf-validator (vcf_validator_linux) supports VCF v4.1-4.3
        # https://github.com/EBIvariation/vcf-validator

        # vcftools is only to validate VCF v4.0

        if vcf_version >= 4.1:
            print("Using vcf_validator_linux...")
            validator_cmd = ["vcf_validator_linux"]
            validator_cmd.append("-i")
            validator_cmd.append(vcf_filepath)
            validator_cmd.append("-l")
            validator_cmd.append('error')
            print("VCF version "+str(vcf_version)+".")
        elif vcf_version >= 4.0:
            print("Using vcftools to validate...")
            validator_cmd = ["vcf-validator"]
            validator_cmd.append(vcf_filepath)
            print("VCF version 4.0.")
        else:
            raise ValueError('VCF Version not in file, or fileformat line malformatted, or not version >=4.0. file format line must be the '
                             'first line of vcf file and in appropriate syntax. Check VCF file specifications: '
                             'https://samtools.github.io/hts-specs/')

        print("Validator command: {}".format(validator_cmd))

        p = subprocess.Popen(validator_cmd,
                             cwd=self.scratch,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT,
                             shell=False)

        validator_output = []
        while True:
            line = p.stdout.readline()
            if not line:
                break
            if line.decode("utf-8").strip().startswith('[info]'):
                validator_output.append(line.decode("utf-8"))

        p.wait()

        validation_output_filename = os.path.join(validation_output_dir, 'vcf_validation.txt')
        file_output_chk = []

        try:
            if validator_output[0][:6] == '[info]':
                # validation by vcf_validator_linux
                validation_output_filename = validator_output[1].split(' ')[6].strip('\n')
                vo = validator_output[2].split(' ')
                file_output_chk = ''.join(vo[9:]).strip('\n')

                if not os.path.exists(validation_output_filename):
                    raise ValueError(validation_output_filename+' does not exist!')

                if not file_output_chk == 'isvalid':
                    print('\n'.join(validator_output))
                    raise ValueError('\n'.join(validator_output))

                #TODO: more detailed validation parsing for vcf_validator_linux
            else:
                if validator_output:
                    with open(validation_output_filename, 'w') as f:
                        for line in validator_output:
                            f.write(str(line))
                        f.close()
                    print('\n'.join(validator_output))
                    raise ValueError('\n'.join(validator_output))
                else:
                    with open(validation_output_filename, 'w') as f:
                        f.write("vcftools used to validate vcf file:\n"+vcf_filepath+"\n\File is validate as of vcf spec v4.0")
                        f.close()

                # TODO: more detailed validation parsing for vcftools
        except IndexError:
            # if vcf file < v4.1, and valid it will produce index error on line 132
            if validator_output:
                with open(validation_output_filename, 'w') as f:
                    for line in validator_output:
                        f.write(str(line))
                    f.close()
                print('\n'.join(validator_output))
                raise ValueError('\n'.join(validator_output))
            else:
                with open(validation_output_filename, 'w') as f:
                    f.write("vcftools used to validate vcf file:\n" + vcf_filepath + "\n\File is validate as of vcf spec v4.0")
                    f.close()

        if not os.path.exists(validation_output_filename):
            print('Validator did not generate log file!')
            raise SystemError("Validator did not generate a log file.")

        log("Validator output filepath: {}".format(validation_output_filename))

        log("Return code from validator {}".format(p.returncode))

        return validation_output_filename

    def _stage_input(self, params):
        # extract file location from input ui parameters
        if params['vcf_staging_file_path'].startswith('/kb/module/test/'):
            # variation utils unit test
            vcf_local_file_path = params['vcf_staging_file_path']

            if vcf_local_file_path.endswith('.gz'):
                with gzip.open(vcf_local_file_path, 'rb') as f_in:
                    with open(vcf_local_file_path[:-3], 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)

                vcf_local_file_path = vcf_local_file_path[:-3]
        else:
            staging_dir = '/staging'
            vcf_local_file_path = os.path.join(staging_dir, params['vcf_staging_file_path'])

        if not os.path.exists(vcf_local_file_path):
            raise OSError('VCF input path does not exist, or is not readable')

        orig_file_path = os.path.join(self.scratch, 'original_' + os.path.basename(vcf_local_file_path))
        print(f'VCF: {vcf_local_file_path} Orig: {orig_file_path}')
        self.original_file = shutil.copy(vcf_local_file_path, orig_file_path)

        # TODO: use data file utils here, upload vcf to shock, use dfu.
        if is_gz_file(vcf_local_file_path):
            # /staging is read only, therefore have to copy before uncompressing
            if not vcf_local_file_path == os.path.join(self.scratch, params['vcf_staging_file_path']):
                copy = shutil.copy(vcf_local_file_path, os.path.join(self.scratch,params['vcf_staging_file_path']))
                unpack = self.dfu.unpack_file({'file_path': copy})
            else:
                unpack = {}
                unpack['file_path'] = os.path.join(self.scratch,params['vcf_staging_file_path'])

            return unpack['file_path']
        else:
            return vcf_local_file_path

    def _validate_assembly_ids(self, params):
        # All chromosome ids from the vcf should be in assembly
        # but not all assembly chromosome ids should be in vcf


        subset = self.wsc.get_object_subset([{
            'included': ['/assembly_ref'],
            'ref': params['genome_ref']
        }])

        self.vcf_info['assembly_ref'] = subset[0]['data']['assembly_ref']

        assembly_chromosome_ids_call = self.wsc.get_object_subset([{
            'included': ['/contigs'],
            'ref': self.vcf_info['assembly_ref']
        }])

        assembly_chromosomes = assembly_chromosome_ids_call[0]['data']['contigs'].keys()
        vcf_chromosomes = self.vcf_info['chromosome_ids']

        chk_assembly_ids =  self._chk_if_vcf_ids_in_assembly(vcf_chromosomes, assembly_chromosomes)

        if isinstance(chk_assembly_ids, list):
            failed_ids = ' '.join(chk_assembly_ids)
            # raise ValueError(f'VCF contig ids: {failed_ids} are not present in assembly.')
            print(f'VCF contig ids: {failed_ids} are not present in assembly.')

        return assembly_chromosomes

    def _validate_sample_ids(self, params):
        # All samples within the VCF file need to be in sample attribute list
        vcf_genotypes = self.vcf_info['genotype_ids']

        sample_ids_subset = self.wsc.get_object_subset([{
            'included': ['/instances'],
            'ref': params['sample_attribute_ref']
        }])

        sample_ids = sample_ids_subset[0]['data']['instances'].keys()

        validate_genotypes = self._validate_vcf_to_sample(vcf_genotypes, sample_ids)

        if isinstance(validate_genotypes, list):
            failed_genos = ' '.join(validate_genotypes)
            # raise ValueError(f'VCF genotypes: {failed_genos} are not present in sample attribute mapping.')
            print(f'VCF genotypes: {failed_genos} are not present in sample attribute mapping.')

        return sample_ids

    def _construct_contig_info(self, params):
        """
            KBaseGwasData.Variations type spec

            /*
               Contig variation data
                 contig_id - contig identifier
                 totalvariants - total number of variants in each contig
                 passvariants - total number of variants that pass quality variation filter in contig
                 length - length of contig from assembly data
             */

             typdef structure {
               string contig_id;
               int totalvariants;
               int passvariants;
               int length; // from assembly
             } contig_info;
        """
        contigs = []

        contig_infos = self.vcf_info['contigs']

        for variant in contig_infos:
            contigs.append(contig_infos[variant])

        return contigs

    def _construct_variation(self, params, contigs_info):
        """
            KBaseGwasData.Variations type spec
             /*
               Variation object data structure
                 num_genotypes - number of total genotypes within variant file
                 num_variants - number of total variants within variant file
                 contigs - list of contig ids and variant information
                 attribute_ref - KBase reference to attribute mapping workspace object
                 genome_ref - KBase reference to genome workspace object
                 assembly_ref - KBase reference to assemebly workspace object
                 vcf_handle_ref - VCF handle reference to VCF file

                 @optional genome_ref
             */
             typedef structure {
               int numgenotypes;
               int numvariants;
               list<contig_info> contigs;
               attribute_ref population; // KBaseExperiments.AttributeMapping
               genome_ref genome_ref; // KBaseGenomes.Genome
               assembly_ref assemby_ref; // KBaseGenomeAnnotations.Assembly
               vcf_handle_ref vcf_handle_ref;
             } Variations;

            :param params: KBase ui input parameters
            :param population: previoiusly constructed sample population data
            :return: constructed variation object (dictionary)
        """

        if not self.vcf_info['file_ref'].startswith(self.scratch):
            new_vcf_file = os.path.join(self.scratch, os.path.basename(self.vcf_info['file_ref']))
            self.vcf_info['file_ref'] = shutil.copy(self.vcf_info['file_ref'], new_vcf_file)

        if not self.vcf_info['file_ref'].endswith('.gz'):
            self.vcf_info['file_ref'] = gzip_file(self.vcf_info['file_ref'])

        #vcf_shock_file_ref = self.dfu.file_to_shock({'file_path': self.vcf_info['file_ref'], 'make_handle': 1})
        vcf_shock_file_ref = self.dfu.file_to_shock({'file_path': self.original_file, 'make_handle': 1})

        #local_md5 = md5_sum_local_file(self.vcf_info['file_ref'])
        local_md5 = md5_sum_local_file(self.original_file)

        shock_md5 = vcf_shock_file_ref['handle']['remote_md5']

        if local_md5 != shock_md5:
            raise ValueError(f'Local md5 {local_md5} does not match shock md5 {shock_md5}')

        if not vcf_shock_file_ref['shock_id']:
            raise ValueError('Unable to upload VCF to Shock!')

        variation_obj = {
            'numgenotypes': int(len(self.vcf_info['genotype_ids'])),
            'numvariants': int(self.vcf_info['total_variants']),
            'contigs': contigs_info,
            'population': params['sample_attribute_ref'],
            'genome_ref': params['genome_ref'],
            # TYPE SPEC CHANGE: need to change type spec to assembly_ref instead of assemby_ref
            'assemby_ref': self.vcf_info['assembly_ref'],
            'vcf_handle_ref': vcf_shock_file_ref['handle']['hid'],
            'vcf_handle': vcf_shock_file_ref['handle']
        }

        return variation_obj

    def _save_var_obj(self, params, var):
        """
        :param params:
        :param var:
        :return:
            DataFileUtils object_info:
                objid - the numerical id of the object.
                name - the name of the object.
                type - the type of the object.
                save_date - the save date of the object.
                ver - the version of the object.
                saved_by - the user that saved or copied the object.
                wsid - the id of the workspace containing the object.
                workspace - the name of the workspace containing the object.
                chsum - the md5 checksum of the object.
                size - the size of the object in bytes.
                meta - arbitrary user-supplied metadata about the object.
        """

        print('Saving Variation to workspace...\n')

        if var:
            if not 'variation_object_name' in params:
                var_obj_name = 'variation_'+str(uuid.uuid4())
            else:
                var_obj_name = params['variation_object_name']

            var_obj_info = self.dfu.save_objects({
                'id': self.dfu.ws_name_to_id(params['workspace_name']),
                'objects': [{
                    'type': 'KBaseGwasData.Variations',
                    'data': var,
                    'name': var_obj_name
                }]
            })[0]

            return var_obj_info
        else:
            raise ValueError('Variation object blank, cannot not save to workspace!')

    def import_vcf(self, params):
        # VCF validation
        # VCF file validation
        file_valid_result = self.validate_vcf(params)
        # VCF file parsing
        self.vcf_info = self._parse_vcf_data(params)
        # Validate vcf chromosome ids against assembly chromosome ids
        self._validate_assembly_ids(params)
        # Validate vcf genotypes against sample meta data ids
        self._validate_sample_ids(params)

        # Variation object construction
        # construct contigs_info
        contigs_info = self._construct_contig_info(params)
        # construct variation
        var = self._construct_variation(params, contigs_info)

        # Save variation object to workspace
        var_wksp_obj = self._save_var_obj(params, var)

        return [var_wksp_obj, var]
