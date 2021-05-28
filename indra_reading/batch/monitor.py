import boto3
import logging
from time import sleep
from os import makedirs, path
from datetime import datetime

from indra.util.aws import get_ids, JobLog, tag_instance

from indra_reading.util import get_s3_job_log_prefix
from indra_reading.batch.util import bucket_name

logger = logging.getLogger('batch_monitor')


class BatchReadingError(Exception):
    pass


class BatchMonitor(object):
    """A monitor for batch jobs.

    Parameters
    ----------
    queue_name : str
        The name of the queue to wait for completion.
    job_list : Optional[list(dict)]
        A list of jobID-s in a dict, as returned by the submit function.
        Example: [{'jobId': 'e6b00f24-a466-4a72-b735-d205e29117b4'}, ...]
        If not given, this function will return if all jobs completed.
    job_base : Optional[str]
        Give the root name of the jobs you want to track.
    log_base : Optional[str]
        Indicate the root name of the location you wish all logs to be stored.
        If you choose to dump logs on s3, this will be the s3 prefix. Note that
        a trailing '/' is NOT assumed.
    """
    def __init__(self, queue_name, job_list=None, job_base=None, log_base=None):

        self.start_time = datetime.now()
        self.queue_name = queue_name

        # Define the various names for this bunch of jobs...
        self.job_base = job_base
        self.log_base = log_base if log_base else '.'

        # Prime some recording containers.
        self.job_list = job_list
        self.job_log_dict = {}

        # Don't start watching jobs added after this command was initialized.
        self.observed_job_def_dict = {}

        self.batch_client = boto3.client('batch')

        self.job_id_list = None
        self._submitter_submitting = False
        return

    def set_submitting(self, status):
        logger.info("%s monitor received that submission status is %s"
                    % (self.queue_name, status))
        if not isinstance(status, bool):
            raise ValueError("`status` must be a bool.")
        self._submitter_submitting = status

    def watch_and_wait(self, poll_interval=10, idle_log_timeout=None,
                       kill_on_log_timeout=False, stash_log_method=None,
                       tag_instances=False, wait_for_first_job=False,
                       dump_size=10000, result_record=None):
        """Return when all jobs are finished.

        If no job list was given, return when all jobs in queue finished.

        Parameters
        ----------
        poll_interval : Optional[int]
            The time delay between API calls to check the job statuses.
        idle_log_timeout : Optional[int] or None
            If not None, then track the logs of the active jobs, and if new
            output is not produced after `idle_log_timeout` seconds, a warning
            is printed. If `kill_on_log_timeout` is set to True, the job will
            also be terminated.
        kill_on_log_timeout : Optional[bool]
            If True, and if `idle_log_timeout` is set, jobs will be terminated
            after timeout. This has no effect if `idle_log_timeout` is None.
            Default is False.
        stash_log_method : Optional[str]
            Select a method to store the job logs, either 's3' or 'local'. If
            no method is specified, the logs will not be loaded off of AWS. If
            's3' is specified, then `log_base` must have been given in
            __init__, as this will indicate where to store the logs.
        tag_instances : bool
            Default is False. If True, apply tags to the instances. This is
            today typically done by each job, so in most cases this should not
            be needed.
        wait_for_first_job : bool
            Don't exit until at least one job has been found. This is good if
            you are monitoring jobs that are submitted periodically, but can be
            a problem if there is a chance you might call this when no jobs
            will ever be run.
        dump_size : int
            Set the size of the log dumps (number of lines). The default is
            10,000.
        result_record : dict
            A dict which will be modified in place to record the results of the
            job.
        """
        logger.info("Given %s jobs to track"
                    % ('no' if self.job_list is None else len(self.job_list)))
        result_record = {} if result_record is None else result_record
        if tag_instances:
            ecs_cluster_name = \
                get_ecs_cluster_for_queue(self.queue_name, self.batch_client)
        else:
            ecs_cluster_name = None
        terminate_msg = 'Job log has stalled for at least %f minutes.'
        terminated_jobs = set()
        found_a_job = False
        while True:
            pre_run = []
            self.job_id_list = get_ids(self.job_list)
            for status in ('SUBMITTED', 'PENDING', 'RUNNABLE', 'STARTING'):
                pre_run += self.get_jobs_by_status(status)
            running = self.get_jobs_by_status('RUNNING')
            failed = self.get_jobs_by_status('FAILED')
            done = self.get_jobs_by_status('SUCCEEDED')

            if len(pre_run + running):
                found_a_job = True

            self.observed_job_def_dict.update(
                self.get_dict_of_job_tuples(pre_run + running)
            )

            logger.info('(%d s)=(tracking: %d, pre: %d, running: %d, '
                        'failed: %d, done: %d)'
                        % ((datetime.now() - self.start_time).seconds,
                           len(self.job_id_list), len(pre_run), len(running),
                           len(failed), len(done)))

            # Check the logs for new output, and possibly terminate some jobs.
            stalled_jobs = self.check_logs(running, idle_log_timeout)
            if idle_log_timeout is not None:
                if kill_on_log_timeout:
                    # Keep track of terminated jobs so we don't send a
                    # terminate message twice.
                    for jid in stalled_jobs - terminated_jobs:
                        self.batch_client.terminate_job(
                            jobId=jid,
                            reason=terminate_msg % (idle_log_timeout/60.0)
                        )
                        logger.info('Terminating %s.' % jid)
                        terminated_jobs.add(jid)

            # Check for end-conditions.
            if found_a_job or not wait_for_first_job:
                if self._submitter_submitting:
                    # Wait for the submitter to be done submitting.
                    logger.info("Submitter is submitting...")
                elif self.job_id_list:
                    if (len(failed) + len(done)) == len(self.job_id_list):
                        logger.info("Submitter status is : %s"
                                    % self._submitter_submitting)
                        logger.info("Total failed and done equals number of "
                                    "original tracked jobs. Ending.")
                        ret = 0
                        break
                else:
                    if (len(failed) + len(done) > 0) and \
                            (len(pre_run) + len(running) == 0):
                        logger.info("No job_id_list, but there are new "
                                    "finished jobs and no running or pre-"
                                    "running jobs. Ending.")
                        ret = 0
                        break

            if tag_instances:
                tag_instances_on_cluster(ecs_cluster_name)

            # Stash the logs of things that have finished so far. Note that
            # jobs terminated in this round will not be picked up until the
            # next round.
            if stash_log_method:
                for job_log in self.job_log_dict.values():
                    self._stash_log(job_log, stash_log_method)

            sleep(poll_interval)

        # Reload the stashed logs, and dump a final record log.
        if stash_log_method:
            failed_jobs = {job['jobId'] for job in failed}
            succeeded_jobs = {job['jobId'] for job in done}
            for job_log in self.job_log_dict.values():
                job_log.load(self._get_log_name(stash_log_method,
                                                job_log.job_name, 'RUNNING'))
                if job_log.job_id in terminated_jobs:
                    label = 'TERMINATED'
                elif job_log.job_id in failed_jobs:
                    label = 'FAILURE'
                elif job_log.job_id in succeeded_jobs:
                    label = "SUCCESS"
                else:
                    label = 'UNKNOWN'
                    logger.warning("Job %s not among terminated, succeeded, "
                                   "or failed..." % job_log.job_id)

                final_log_name = self._get_log_name(stash_log_method,
                                                    job_log.job_name, label)
                job_log.dump(final_log_name, append=False)
                job_log.clear_lines()

        result_record['terminated'] = terminated_jobs
        result_record['failed'] = failed
        result_record['succeeded'] = done

        return ret

    def _stash_log(self, job_log, stash_log_method):
        log_name = self._get_log_name(stash_log_method, job_log.job_name,
                                      'RUNNING')
        job_log.dump(log_name)
        job_log.clear_lines()
        return log_name

    def _get_log_name(self, stash_log_method, job_name, label=''):
        log_name = '%s_stash.log' % job_name
        if not self.log_base:
            raise ValueError("You cannot stash logs without specifying a base "
                             "directory for the logs: log_base.")
        if stash_log_method == 's3':
            s3_prefix = get_s3_job_log_prefix(self.log_base, job_name,
                                              job_queue=self.queue_name)
            log_name = (label + '_' if label else '') + log_name
            log_name = 's3:%s/%s%s' % (bucket_name, s3_prefix, log_name)
        elif stash_log_method == 'local':
            prefix = self.log_base
            if prefix is None:
                prefix = 'batch_stash'
            dirname = '%s_job_logs' % prefix
            if not path.exists(dirname):
                makedirs(dirname)
            log_name = path.join(dirname, log_name)
        else:
            raise ValueError("Invalid log stash method: %s"
                             % stash_log_method)
        return log_name

    def get_jobs_by_status(self, status):
        res = self.batch_client.list_jobs(jobQueue=self.queue_name,
                                          jobStatus=status, maxResults=10000)
        jobs = res['jobSummaryList']
        if self.job_base:
            jobs = [job for job in jobs if
                    job['jobName'].startswith(self.job_base)]
        if self.job_id_list is not None:
            jobs = [job_def for job_def in jobs
                    if job_def['jobId'] in self.job_id_list]
        return jobs

    def check_logs(self, job_defs, idle_log_timeout):
        """Updates the job_log_dict."""
        stalled_jobs = set()

        # Check the status of all the jobs we're tracking.
        for job_def in job_defs:
            try:
                # Get the job id.
                jid = job_def['jobId']
                now = datetime.utcnow()
                if jid not in self.job_log_dict.keys():
                    # If the job is new...
                    logger.info("Adding job %s to the log job_log at %s."
                                % (jid, now))
                    # Instantiate a new job_log.
                    job_log = JobLog(job_def)
                    self.job_log_dict[jid] = job_log
                else:
                    job_log = self.job_log_dict[jid]

                pre_len = len(job_log)
                job_log.get_lines()
                post_len = len(job_log)

                if post_len and pre_len == post_len:
                    # If the job log hasn't changed, announce as such, and
                    # check to see if it has been the same for longer than
                    # stall time.
                    check_dt = now - job_log.latest_timestamp
                    logger.warning(('Job \'%s\' has not produced output for '
                                    '%d seconds.')
                                   % (job_def['jobName'], check_dt.seconds))
                    if idle_log_timeout and check_dt.seconds > idle_log_timeout:
                        logger.warning("Job \'%s\' has stalled."
                                       % job_def['jobName'])
                        stalled_jobs.add(jid)
            except Exception as e:
                # Sometimes due to sync et al. issues, a part of this will fail
                # Such things are usually transitory issues so we keep trying.
                logger.error("Failed to check log for: %s" % str(job_def))
                logger.exception(e)

        # Pass up the set of job id's for stalled jobs.
        return stalled_jobs

    @staticmethod
    def get_dict_of_job_tuples(job_defs):
        return {jdef['jobId']: [(k, jdef[k]) for k in ['jobName', 'jobId']]
                for jdef in job_defs}


def _get_job_ids_to_stash(job_def_list, stashed_id_set):
    return [job_def['jobId'] for job_def in job_def_list
            if job_def['jobId'] not in stashed_id_set]


def get_ecs_cluster_for_queue(queue_name, batch_client=None):
    """Get the name of the ecs cluster using the batch client."""
    if batch_client is None:
        batch_client = boto3.client('batch')

    queue_resp = batch_client.describe_job_queues(jobQueues=[queue_name])
    if len(queue_resp['jobQueues']) == 1:
        queue = queue_resp['jobQueues'][0]
    else:
        raise BatchReadingError('Error finding queue with name %s.'
                                % queue_name)

    compute_env_names = queue['computeEnvironmentOrder']
    if len(compute_env_names) == 1:
        compute_env_name = compute_env_names[0]['computeEnvironment']
    else:
        raise BatchReadingError('Error finding the compute environment name '
                                'for %s.' % queue_name)

    compute_envs = batch_client.describe_compute_environments(
        computeEnvironments=[compute_env_name]
    )['computeEnvironments']
    if len(compute_envs) == 1:
        compute_env = compute_envs[0]
    else:
        raise BatchReadingError("Error getting compute environment %s for %s. "
                                "Got %d environments instead of 1."
                                % (compute_env_name, queue_name,
                                   len(compute_envs)))

    ecs_cluster_name = path.basename(compute_env['ecsClusterArn'])
    return ecs_cluster_name


def tag_instances_on_cluster(cluster_name, project='cwc'):
    """Adds project tag to untagged instances in a given cluster.

    Parameters
    ----------
    cluster_name : str
        The name of the AWS ECS cluster in which running instances
        should be tagged.
    project : str
        The name of the project to tag instances with.
    """
    # Get the relevant instance ids from the ecs cluster
    ecs = boto3.client('ecs')
    task_arns = ecs.list_tasks(cluster=cluster_name)['taskArns']
    if not task_arns:
        return
    tasks = ecs.describe_tasks(cluster=cluster_name, tasks=task_arns)['tasks']
    container_instances = ecs.describe_container_instances(
        cluster=cluster_name,
        containerInstances=[task['containerInstanceArn'] for task in tasks]
    )['containerInstances']
    ec2_instance_ids = [ci['ec2InstanceId'] for ci in container_instances]

    # Instantiate each instance to tag as a resource and create project tag
    for instance_id in ec2_instance_ids:
        tag_instance(instance_id, project=project)
    return
