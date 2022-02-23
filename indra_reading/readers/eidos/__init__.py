import re
import glob
import json
import logging
from os import path, remove, listdir
from indra.sources import eidos
from indra.config import get_config
from indra.literature.pmc_client import extract_text
from indra.sources.eidos.cli import extract_from_directory
from indra_reading.readers.core import Reader
from indra_reading.readers.util import get_dir
from indra_reading.readers.content import Content


logger = logging.getLogger(__name__)


class EidosReader(Reader):
    name = 'EIDOS'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.num_input = 0
        self.input_dir = get_dir(self.tmp_dir, 'input')
        self.output_dir = get_dir(self.tmp_dir, 'output')

    @classmethod
    def get_version(cls):
        jar_name = path.basename(get_config('EIDOSPATH'))
        return re.match(r'eidos-assembly-(.+).jar', jar_name).groups()[0]

    def prep_input(self, content_iter):
        logger.info('Prepping input.')
        for content in content_iter:
            # If it's an NXML, we get the raw text and save it as new content
            if content.is_format('nxml'):
                txt = extract_text(content.get_text())
                content = \
                    Content.from_string(str(content.get_id()),
                                        'txt', txt)
            quality_issue = self._check_content(content.get_text())
            if quality_issue is not None:
                logger.warning('Skipping %s due to: %s'
                               % (content.get_id(), quality_issue))
                continue

            new_fpath = content.copy_to(self.input_dir)
            self.num_input += 1
            logger.debug('%s saved for reading by Eidos.' % new_fpath)
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
        logger.info('Getting Eidos outputs.')
        for json_file in glob.glob(path.join(self.output_dir, '*.jsonld')):
            # We do splitext to remove double extensions like .txt.jsonld
            content_id = path.splitext(
                path.splitext(path.basename(json_file))[0])[0]
            logger.info('Content ID: %s' % content_id)
            with open(json_file, 'r') as fh:
                try:
                    content = json.load(fh)
                except json.JSONDecodeError:
                    content = None
            self.add_result(content_id, content)
        return self.results

    def _read(self, content_iter, verbose=False, log=False):
        ret = []
        self.prep_input(content_iter)

        # Make sure there is something to read before we start up Eidos.
        if not self.num_input:
            return ret

        extract_from_directory(self.input_dir, self.output_dir)

        # Get the output
        ret = self.get_output()
        self.clear_input()

        return ret

    @staticmethod
    def parse_results(content):
        from indra.tools.assemble_corpus import filter_grounded_only
        ep = eidos.process_json_bio(content)
        ep.statements = filter_grounded_only(ep.statements)
        return ep
