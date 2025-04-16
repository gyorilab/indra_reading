import click
from pathlib import Path

from zipfile import ZipFile

import boto3


HERE = Path(__file__).parent.resolve()


def upload_function(fun_file: str, fun_name: str):
    """Upload the lambda function by pushing a zip file to Lambda

    Parameters
    ----------
    fun_file : str
        Name of a python file containing lambda function.
    fun_name : str
        Name of a lambda function as specified on AWS Lambda.

    """
    lambda_client = boto3.client("lambda")
    fun_path = HERE.joinpath(fun_file)
    if not fun_path.exists():
        raise FileNotFoundError(f"Lambda function file {fun_path} not found.")

    # Create a zip file with the lambda function using the following archive
    # structure:
    # <lambda_fun_name>/<lambda_fun_file>
    with ZipFile(HERE.joinpath("lambda.zip"), "w") as zf:
        zf.write(filename=fun_path, arcname=f"{fun_name}/{fun_path.name}")

    # Upload the zip file to AWS Lambda
    with open(HERE.joinpath("lambda.zip"), "rb") as zf:
        ret = lambda_client.update_function_code(
            ZipFile=zf.read(), FunctionName=fun_name
        )
        print(ret)

    return


# Setup click command line interface
@click.command()
@click.argument("lambda_fun_file")
@click.argument("lambda_fun_name")
def main(lambda_fun_file: str, lambda_fun_name: str):
    """Upload a lambda function to AWS Lambda.

    lambda_fun_file :
        Path to a python file containing lambda function.
    lambda_fun_name :
        Name of a lambda function as specified on AWS Lambda.
    """
    upload_function(lambda_fun_file, lambda_fun_name)

    return


if __name__ == "__main__":
    main()
