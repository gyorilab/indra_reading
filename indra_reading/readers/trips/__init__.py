import os
import re
import socket
import random
import logging
import threading
import subprocess as sp
from os.path import expanduser

from unidecode import unidecode
from contextlib import closing
from datetime import datetime, timedelta, timezone

from indra.resources.greek_alphabet import greek_alphabet
from indra_reading.readers.core import Reader

from indra.sources.trips import client, process_xml


logger = logging.getLogger(__name__)

startup_path = '/sw/drum/bin/startup.sh'
service_endpoint = 'drum'
DRUM_DOCKER = '292075781285.dkr.ecr.us-east-1.amazonaws.com/drum'


def find_free_ports():
    """Find ports that are unused.

    The order is randomized to minimize the chances of race-condition overlaps.
    """
    # First try to determine port by using environment variables
    job_group_id = os.environ.get('JOB_GROUP_ID')
    job_id = os.environ.get('JOB_ID')
    if job_group_id and job_id:
        logger.info("Using JOB_GROUP_ID and JOB_ID to determine port.")
        port = 6200 + 1000 * int(job_group_id) + int(job_id)
        logger.info("Using port %d." % port)
        yield port
    else:
        logger.info("No environment variables for determining the port.")
    logger.info("Using random port.")
    ports = list(range(1, 65536))
    random.shuffle(ports)
    for port in ports:
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sok:
            res = sok.connect_ex(('localhost', port))
            if res != 0:
                yield port


class TripsStartupError(Exception):
    pass


def _start_trips():
    # Start trips running
    for port in find_free_ports():
        port_failure = False
        if os.environ.get("IN_TRIPS_DOCKER", 'false') == 'true':
            logger.info("Attempting to starting up a TRIPS service from "
                        "within the docker on port %d." % port)
            p = sp.Popen([expanduser('~/startup_trips.sh'), str(port)],
                         stdout=sp.PIPE, stderr=sp.STDOUT)
        else:
            logger.info("Starting up a TRIPS service using drum docker.")
            p = sp.Popen(['docker', 'run', '-it', '-p', '%d:80' % port,
                          '--entrypoint', '/sw/drum/bin/startup.sh',
                          DRUM_DOCKER],
                         stdout=sp.PIPE, stderr=sp.STDOUT)
        service_host = 'http://localhost:%d/cgi/' % port

        # Wait for the service to be ready
        for log_line in _tail_trips(p):
            if 'can\'t bind to port' in log_line:
                port_failure = True
            if log_line == 'Ready':
                # TRIPS is ready to read and we can continue on.
                break
        else:
            logger.error("TRIPS failed to start on port %d." % port)
            if port_failure:
                # If the failure due to wrong port, try another port.
                continue
            else:
                # Otherwise give up.
                raise TripsStartupError("Trips failed to start up.")

        # The above for-loop existed without going to else, indicating
        # successful startup.
        break
    else:
        # We exhausted all possible ports.
        raise TripsStartupError("Could not start TRIPS, all ports appear "
                                "to be busy.")
    logger.info("Service has started up.")
    return p, service_host


def _kill_trips():
    """Kill all running instances of trips-drum, indiscriminately.

    Specifically, this runs the equivalent of the two bash commands:
    $ pkill -f trips-drum
    $ pkill -f lighttpd

    Killing the trips-drum code and the hosting service respectively.
    """
    sp.run(['pkill', '-f', 'trips-drum'])
    sp.run(['pkill', '-f', 'trips-drum'])


class TripsReader(Reader):
    """A wrapper around the TRIPS reading system."""
    name = 'TRIPS'
    result_format = 'xml'

    def __init__(self, *args, **kwargs):
        self.version = self.get_version()
        super(TripsReader, self).__init__(*args, **kwargs)
        self.running = False
        self.stopping = False
        return

    def _monitor_trips_service(self, proc):
        self.running = True
        for _ in _tail_trips(proc):
            if self.stopping:
                logger.info("Got stop signal. Stopping.")
                break
        logger.info("Waiting for TRIPS process to join.")
        try:
            proc.communicate(timeout=10)
        except sp.TimeoutExpired:
            logger.warning("TRIPS did not end.")
            self.running = False
            return
        if proc.returncode:
            logger.error("TRIPS ended with return code %d." % proc.returncode)
        else:
            logger.info("TRIPS ended without error.")
        logger.info("TRIPS is no longer running.")
        self.running = False

    def _read(self, content_iter, verbose=False, log=False, n_per_proc=None):

        # Process all the content.
        th = None
        trips_started = False
        for content in content_iter:
            if not trips_started:
                p, service_host = _start_trips()

                # Set up the trips monitor
                th = threading.Thread(target=self._monitor_trips_service,
                                      args=[p])
                th.start()
                trips_started = True

            if not self.running:
                logger.error("Breaking loop: trips is down.")
                break

            # Clean up the text string a bit.
            # - remove all excess white space.
            # - remove special greek letters
            # - remove all special unicode, replace with ascii
            raw_text = content.get_text()
            raw_text = re.sub('\s+', ' ', raw_text)
            for greek_letter, spelled_letter in greek_alphabet.items():
                raw_text = raw_text.replace(greek_letter, spelled_letter)
            text = unidecode(raw_text)

            # Process the text
            html = client.send_query(text, service_host=service_host,
                                     service_endpoint=service_endpoint)
            if html:
                xml = client.get_xml(html)
                self.add_result(content.get_id(), xml)
            else:
                self.add_result(content.get_id(), None)

        # Stop TRIPS if it hasn't stopped already.
        logger.info("Killing TRIPS")
        _kill_trips()  # Kill all instances of trips-drum.

        logger.info("Signalling observation loop to stop.")
        self.stopping = True  # Sends signal to the loop to stop

        if th is not None:
            logger.info("Waiting for observation loop thread to join.")
            th.join(timeout=5)
            if th.is_alive():
                logger.warning("Thread did not end, TRIPS is still running.")

        return self.results

    @classmethod
    def get_version(cls):
        """Determine the current version of TRIPS being used."""
        git_date_cmd = ['git', 'log', '-1', '--format=%cd']
        if os.environ.get("IN_TRIPS_DOCKER", "false") == "true":
            curdir = os.getcwd()
            try:
                # Get the date of the last commit from git in drum.
                os.chdir('/sw/drum')
                res = sp.run(git_date_cmd, stdout=sp.PIPE)
            finally:
                os.chdir(curdir)
        else:
            res = sp.run(['docker', 'run', DRUM_DOCKER, '-c',
                          ' '.join(['cd', '/sw/drum;'] + git_date_cmd)],
                         stdout=sp.PIPE)

        # Format that string into a datetime and standardize to utc.
        d = datetime.strptime(res.stdout.decode('utf-8').strip(),
                              '%a %b %d %H:%M:%S %Y %z')
        d.astimezone(tz=timezone(timedelta(0)))

        # Create a formatted string as the version.
        version = d.strftime('%Y%b%d')

        return version

    @staticmethod
    def parse_results(reading_content):
        return process_xml(reading_content)


def _tail_trips(proc):
    for line in iter(proc.stdout.readline, b''):
        log_line = line.strip().decode('utf-8')
        logger.info('TRIPS: ' + log_line)
        yield log_line
