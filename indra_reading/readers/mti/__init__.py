import logging
from indra.databases import mesh_client

logger = logging.getLogger(__name__)


def get_config_extended(key):
    """Return config either from INDRA, environemnt, or AWS SSM."""
    from indra.config import get_config
    val = get_config(key)
    if val:
        logger.info('Got %s from environment' % key)
        return val
    import boto3
    client = boto3.client('ssm')
    response = client.get_parameter(Name=key, WithDecryption=True)
    val = response['Parameter']['Value']
    logger.info('Got %s from SSM' % key)
    return val


try:
    # We get MTI configuration parameters first
    mti_email = get_config_extended('MTI_EMAIL')
    mti_api_key = get_config_extended('MTI_API_KEY')
    mti_jars_path = get_config_extended('MTI_JARS_PATH')

    # We next need to take care of setting the CLASSPATH and then importing
    # jnius before the other imports
    from os import environ
    environ['CLASSPATH'] = f'{mti_jars_path}/*'
    from jnius import autoclass
    MTI_AVAILABLE = True
except:
    logger.warning("Unable to access secure parameters; "
                   "MTI reading will not be available.")
    MTI_AVAILABLE = False

import re
import html
import glob
from os import path, remove, listdir
from collections import defaultdict
from indra_reading.readers.core import Reader
from indra_reading.readers.util import get_dir


def sanitize_text(txt):
    """MTI needs single-line text and errors on non-ASCII."""
    txt = html.unescape(txt)
    txt = re.sub(r'\n', ' ', txt)
    txt = re.sub(r'[^\x00-\x7F]+', ' ', txt)
    return txt


def has_error(line):
    return '*** ERROR ***' in line


class MtiUnavailableError(Exception):
    pass


class MTIReader(Reader):
    name = 'MTI'
    results_type = 'mesh_terms'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_input = 0
        self.input_dir = get_dir(self.tmp_dir, 'input')
        self.output_dir = get_dir(self.tmp_dir, 'output')

    @classmethod
    def get_version(cls):
        return '1.0'

    def prep_input(self, content_iter):
        logger.info('Prepping input.')
        abs_file = path.join(self.input_dir, 'abstracts.txt')
        abs_file = path.realpath(path.expanduser(abs_file))

        # MTI takes a single text file with multiple IDs and text
        # contents when running in batch mode. Here we compile
        # that single file.
        with open(abs_file, 'w') as fh:
            for content in content_iter:
                # If it's an NXML, we skip it
                if content.is_format('nxml'):
                    continue
                quality_issue = self._check_content(content.get_text())
                if quality_issue is not None:
                    logger.warning('Skipping %s due to: %s'
                                   % (content.get_id(), quality_issue))
                    continue
                self.num_input += 1
                fh.write('UI  -  %s\n' % content.get_id())
                fh.write('AB  -  %s\n\n' % sanitize_text(content.get_text()))
        return

    def clear_input(self):
        """Remove all the input files (at the end of a reading)."""
        for item in listdir(self.input_dir):
            item_path = path.join(self.input_dir, item)
            if path.isfile(item_path):
                remove(item_path)
                logger.debug('Removed input %s.' % item_path)
        return

    def get_output(self):
        """Get the output of a reading job as a list of filenames."""
        logger.info('Getting MTI outputs.')
        for txt_file in glob.glob(path.join(self.output_dir, '*.txt')):
            logger.info('Processing %s' % txt_file)
            content_id = path.splitext(path.basename(txt_file))[0]
            logger.info('Content ID: %s' % content_id)
            with open(txt_file, 'r') as fh:
                content = fh.read()
            if content and content.startswith('ERROR'):
                logger.info('MTI error: "%s"' % content)
                continue
            self.add_result(content_id, content)
        return self.results

    def _read(self, content_iter, verbose=False, log=False):
        if not MTI_AVAILABLE:
            raise MtiUnavailableError("MTI is not available for reading.")
        logger.info('Running MTI.')
        ret = []
        self.prep_input(content_iter)

        if not self.num_input:
            return ret

        # We can now retrieve the prepared input file and call
        # MTI batch
        abs_file = path.join(self.input_dir, 'abstracts.txt')
        abs_file = path.realpath(path.expanduser(abs_file))
        logger.info('Instantiating MTI GenericBatchNew class.')
        batch = autoclass('GenericBatchNew')()
        logger.info('Calling MTI batch processor.')
        result = batch.processor(["--email", mti_email, abs_file],
                                 mti_api_key)
        # If there is an error, MTI just returns a string
        # starting with ERROR
        if result.startswith('ERROR'):
            logger.error('MTI returned with error: "%s"' % result)
            return ret

        logger.info('MTI succeeded.')

        # Here we take apart the response by content ID so that we
        # can create separate output files for each content
        result_by_id = defaultdict(list)
        for line in result.splitlines():
            if has_error(line):
                logger.info('Skipping line with error: %s' % line)
                continue
            parts = line.split('|')
            content_id = parts[0]
            result_by_id[content_id].append(line)
        logger.info('Got results for %s IDs' % len(result_by_id))
        for content_id, res in result_by_id.items():
            out_file = path.join(self.output_dir,
                                 '%s.txt' % content_id)
            with open(out_file, 'w') as fh:
                for line in res:
                    fh.write('%s\n' % line)
        # Get the output
        ret = self.get_output()
        self.clear_input()
        return ret

    @staticmethod
    def parse_results(content):
        """Get terms from a single MTI output"""
        if not content:
            return []

        terms = set()

        # Split content into non-empty lines
        lines = content.splitlines()
        for line in lines:
            topic = line.split('|')[1]

            # Look for mesh ID of the topic
            mesh_id_str = mesh_client.get_mesh_id_name(topic)[0]
            if not mesh_id_str:
                logger.warning('Mesh ID not found for "%s"' % topic)
                continue 

            # Add mesh ID as a number without prefix
            assert mesh_id_str[0] in ['C', 'D'], \
                f"Supposedly impossible mesh ID found: {mesh_id_str}"
            is_concept = (mesh_id_str[0] == 'C')
            mesh_id = int(mesh_id_str[1:])
            terms.add((mesh_id, is_concept))

        return list(terms)
