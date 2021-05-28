Readers
=======

This is the control center for implementing reader modules. Each reader has its own
unique API and ways of dumping its results. To fit into our reading framework, each
reader must have these unique features smoothed into a uniform API. Defined in this
directory are the implementations for each reader we currently support, indicated by
the directory name.

In addition, many of the readers we use have very particular dependencies, and often
installing multiple readers on the same system is not practical or even possible. Thus
each reader can have a docker image defined for it, with `indra_db` and other
necessary modules installed atop another docker image.

Prerequisites
-------------
Before a reader is added, it should have a processor defined in INDRA, to extract
Statements from the output (MTI is a special case). You must also be able to install
the reading system or have a docker image that you can build atop that can support
at least Python 3.6.

Maintaining Docker Images
-------------------------
Dockerfile's can be constructed from dockerfile fragments provided, or from scratch.
Each reader directory can contain a dockerfile. You can build the docker images by
using the `update_dockers.py` function:
```bash
python update_dockers.py --help
```
Options vary depending on the reader being updated. The actual building of the
docker images happens on AWS CodeBuild, so sufficient credentials will be required
to run the builds.

The arguments are inferred from the dockerfile's `ARG` lines, converted to lowercase
and supplied using the `--argument` convention. Defaults are set on CodeBuild. The
dockerfiles and buildspecs are pushed to S3 in a zip file (one for each reader), from
which they are read by CodeBuild.

Adding a New Reader
-------------------
Readers are implemented using class inheritance, with the basic API defined in
`core.py` and all reader classes inheriting from the `Reader` class.

