import os
import boto3
from datetime import datetime
from time import sleep

BATCH = boto3.client("batch")

QUEUE_NAME = "run_db_lite_queue"
JOB_NAME = "monitor_db_reading_%s" % datetime.now().strftime("%Y%m%d")
JOB_DEF = "run_db_lite_jobdef"
PROJECT = "aske"
PURPOSE = "monitor_reading"


def lambda_handler(event, context):
    print("Getting job status from previous run...")
    response = BATCH.list_jobs(
        jobQueue=QUEUE_NAME, filters=[
            {"name": "JOB_NAME", "values": ["monitor_db_reading_*"]}
        ]
    )
    if response["jobSummaryList"]:
        # Most recent job is first in list
        status = response["jobSummaryList"][0]["status"]
        if status not in ["SUCCEEDED", "FAILED"]:
            print("Previous job is not finished, exiting...")
            return "DONE"

    print("Previous job is done, starting new job...")
    core_command = f"indra-db reading run new --project-name {PROJECT}"
    ret = BATCH.submit_job(
        jobName=JOB_NAME,
        jobQueue=QUEUE_NAME,
        jobDefinition=JOB_DEF,
        containerOverrides={
            "command": [
                "python3",
                "-m",
                "indra.util.aws",
                "run_in_batch",
                core_command,
                "--project",
                PROJECT,
                "--purpose",
                PURPOSE,
            ],
        },
    )
    jobId = ret["jobId"]

    print("Result from job submission:\n%s" % jobId)

    return "DONE"
