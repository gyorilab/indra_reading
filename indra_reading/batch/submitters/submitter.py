import boto3
import logging
from time import sleep
from threading import Thread

from indra.util.aws import get_batch_command, kill_all

from indra_reading.util import get_s3_and_job_prefixes

from indra_reading.batch.util import get_environment
from indra_reading.batch.monitor import BatchMonitor

logger = logging.getLogger('batch_submitter_core')


JOB_STATES = {'pre': ['SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING'],
              'running': ['RUNNING'], 'done': ['SUCCEEDED'],
              'failed': ['FAILED']}


class Submitter:
    """This is the core architecture for managing a suite of jobs on AWS Batch.

    An instance of this class is designed to manage a run of jobs on Batch,
    generally with some logical connection, such as a reading by certain readers
    of certain content. Each such job is identified most specifically by a
    `basename`. A `group_name` may also be specified if jobs fall within a
    certain hierarchy, for example multiple readers managed separately over the
    same body of content.

    The class will handle submitting multiple kinds of jobs to batch to various
    queues and using various job defs. It will then track those jobs, including
    monitoring their logs for a stalled job. There is an option to kill the
    job if it does not produce any logs for a specified length of time.

    On its own this class is not ready to deploy jobs, and it is assumed that a
    sub-class will be written which defines certain specific features of the
    types of jobs being run.

    To simply run all the jobs, use the `run` method.

    Parameters
    ----------
    basename : str
        The name of this particular job.
    group_name : str
        (optional) Batches of jobs may be grouped by some common quality, such
        as a corpus of content on which different readers are applied, or which
        is broken into smaller chunks, or different attempts to run preassembly.
    project_name : str
        Jobs on AWS will be tagged with `project` to track which grants pay for
        the jobs.
    job_timeout : int
        The maximum number of seconds the job will be allowed to run. This
        overrides any value defined in the job def config.
    """
    job_class = NotImplemented

    _purpose = NotImplemented

    # The job queue on which these jobs will be submitted.
    _job_queue_dict = NotImplemented

    # A dictionary of job_def names as keys, with a list of applicable readers
    # as the values.
    _job_def_dict = NotImplemented

    def __init__(self, basename, group_name=None, project_name=None,
                 job_timeout=None, **options):
        self.basename = basename
        self.group_name = group_name
        self.s3_base, self.job_base = \
            get_s3_and_job_prefixes(self.job_class, basename, group_name)
        self.project_name = project_name
        self.job_timeout_override = job_timeout
        self.job_lists = {q_name: [] for q_name in self._job_queue_dict.keys()}
        self.options = options
        self.running = None
        self.submitting = False
        self.monitors = {}
        for queue_name in self._iter_over_select_queues():
            self.monitors[queue_name] = \
                BatchMonitor(queue_name, self.job_lists[queue_name],
                             self.job_base, self.s3_base)
        self.max_jobs = None
        return

    def get_job_counts_by_status(self):
        """Get a dictionary of jobs based on their status."""
        jobs_in_state = dict.fromkeys(JOB_STATES.keys(), 0)
        for q_name in self._iter_over_select_queues():
            for job_state, aws_states in JOB_STATES.items():
                for stat in aws_states:
                    jobs_in_state[job_state] \
                        += len(self.monitors[q_name].get_jobs_by_status(stat))
        return jobs_in_state

    def set_max_jobs(self, num_jobs):
        """Set the maximum number of jobs submitted at one time.

        This only has an effect when jobs are being submitted incrementally, and
        it only affects when new jobs are submitted. Thus, if this is set after
        several jobs are running it will only prevent new jobs being submitted
        until jobs have finished such that only `num_jobs` - 1 remain.
        """
        self.max_jobs = num_jobs

    def _iter_over_select_queues(self):
        raise NotImplementedError

    def set_options(self, **kwargs):
        """Set the options of reading job."""
        # This should be more specifically implemented in a child class.
        self.options = kwargs
        return

    def _iter_job_queue_def_commands(self, *args):
        # Group the readings into batches that share both a queue and a job_def.
        for job_def, jd_job_type_list in self._job_def_dict.items():
            for job_queue, jq_job_type_list in self._job_queue_dict.items():

                # Figure out the addition to the command that will be necessary
                # to select which jobs are run. If none apply, skip this combo
                # of queue and job def.
                job_type_set = set(jd_job_type_list) & set(jq_job_type_list)
                job_name, cmd = self._get_command(job_type_set, *args)
                if not cmd:
                    continue

                for arg in cmd:
                    if not isinstance(arg, str):
                        logger.warning("Argument of command is not a string: %s"
                                       % repr(arg))
                yield job_name, cmd, job_def, job_queue

    def _get_command(self, job_type_set, *args):
        raise NotImplementedError

    def set_monitors_submitting(self, status):
        logger.info("Telling monitors submission status is %s." % status)
        for monitor in self.monitors.values():
            monitor.set_submitting(status)

    def submit_jobs(self, *args, num_tries=1, stagger=0):
        """Submit all the jobs to batch.

        Parameters
        ----------
        num_tries : int
            The number of times a job may be attempted.
        stagger : float
            The number of seconds to wait between job submissions.

        Returns
        -------
        job_lists : dict{queue_name: list[str]}
            A dict of lists of job id strings, keyed by the name of each queue
            used.
        """
        # Get environment variables
        environment_vars = get_environment()

        # Iterate over the list of PMIDs and submit the job in chunks
        batch_client = boto3.client('batch', region_name='us-east-1')

        # Check to see if we've already been given a signal to quit.
        if self.running is None:
            self.running = True
        elif not self.running:
            return None

        self.set_monitors_submitting(True)
        try:
            for job_args in self._iter_job_args(*args):
                # Check for a stop signal
                if not self.running:
                    logger.info("Running was switched off, discontinuing...")
                    break

                cmd_iter = self._iter_job_queue_def_commands(*job_args)
                for job_name, cmd, job_def, job_queue in cmd_iter:
                    # Wait for there to be enough jobs to submit. Wait
                    # increases exponentially.
                    if self.max_jobs is not None:
                        job_counts = self.get_job_counts_by_status()
                        jobs_on_tab = job_counts['pre'] + job_counts['running']
                        wait = 10
                        n_sleeps = 0
                        while jobs_on_tab > self.max_jobs:
                            sleep_time = wait * (2**n_sleeps)
                            logger.info(f"Waiting {sleep_time} seconds for "
                                        f"more {jobs_on_tab - self.max_jobs} "
                                        f"jobs to be finish...")
                            sleep(sleep_time)

                    # Build the command.
                    command_list = get_batch_command(cmd, purpose=self._purpose,
                                                     project=self.project_name)
                    logger.info('Command list: %s' % str(command_list))

                    # Submit the job.
                    kwargs = {}
                    if self.job_timeout_override is not None:
                        kwargs['timeout'] = \
                            {'attemptDurationSeconds': self.job_timeout_override}
                    job_info = batch_client.submit_job(
                        jobName=job_name,
                        jobQueue=job_queue,
                        jobDefinition=job_def,
                        containerOverrides={
                            'environment': environment_vars,
                            'command': command_list},
                        retryStrategy={'attempts': num_tries},
                        **kwargs
                    )

                    # Record the job id.
                    logger.info("submitted...")
                    self.job_lists[job_queue].append(
                        {k: job_info[k] for k in ['jobId', 'jobName']}
                    )
                    logger.info("Sleeping for %d seconds..." % stagger)
                    sleep(stagger)
        finally:
            self.set_monitors_submitting(False)
        return self.job_lists

    def _iter_job_args(self, *args):
        raise NotImplementedError

    def watch_and_wait(self, poll_interval=10, idle_log_timeout=None,
                       kill_on_timeout=False, stash_log_method=None,
                       tag_instances=False, kill_on_exception=True, **kwargs):
        """This provides shortcut access to the wait_for_complete_function."""

        def wait_thread(monitor):
            try:
                wait_res = monitor.watch_and_wait(
                    poll_interval=poll_interval,
                    idle_log_timeout=idle_log_timeout,
                    kill_on_log_timeout=kill_on_timeout,
                    stash_log_method=stash_log_method,
                    tag_instances=tag_instances,
                    **kwargs
                )
            except (BaseException, KeyboardInterrupt) as e:
                logger.error("Exception in wait_for_complete:")
                logger.exception(e)
                if kill_on_exception:
                    logger.info("Killing all my jobs...")
                    kill_all(monitor.queue_name,
                             kill_list=[j for l in self.job_lists.values()
                                        for j in l],
                             reason='Exception in monitor, jobs aborted.')
                raise e
            return wait_res

        res = []
        logger.info("Running %d active/%d monitors."
                    % (len(self.monitors), len(self._job_queue_dict)))
        if len(self.monitors) == 1:
            res.append(wait_thread(list(self.monitors.values())[0]))
        else:
            threads = []
            for m in self.monitors.values():
                th = Thread(target=wait_thread, args=[m])
                th.start()
                threads.append(th)

            for th in threads:
                th.join()

        return res

    def run(self, *args, stagger=0, **wait_params):
        """Run this submission all the way.

        This method will run both `submit_jobs` and `watch_and_wait`,
        blocking on the latter.
        """

        submit_thread = Thread(target=self.submit_jobs, args=args,
                               kwargs={'stagger': stagger}, daemon=True)
        submit_thread.start()
        try:
            logger.info("Waiting for just a sec...")
            sleep(1)
            wait_params['wait_for_first_job'] = True
            wait_params['kill_on_exception'] = True
            self.watch_and_wait(**wait_params)
            submit_thread.join(0)
            if submit_thread.is_alive():
                logger.warning("Submit thread is still running even after job "
                               "completion.")
        except BaseException as e:
            logger.error("Watch and wait failed...")
            logger.exception(e)
        finally:
            logger.info("Aborting jobs...")
            # Send a signal to the submission loop (on a thread) to stop.
            self.running = False
            submit_thread.join()
            print(submit_thread.is_alive())

        self.running = None
        return submit_thread
