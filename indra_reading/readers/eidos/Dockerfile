FROM 292075781285.dkr.ecr.us-east-1.amazonaws.com/indra_db:latest
ENV EIDOSPATH /sw/eidos-assembly-0.2.3-SNAPSHOT.jar
RUN rm -r /sw/reach && \
    rm -r /sw/sparser && \
    rm bionetgen.tar.gz && \
    rm -r BioNetGen*
ADD eidos-assembly-0.2.3-SNAPSHOT.jar $EIDOSPATH
# Create geonames resource to avoid runtime download
RUN mkdir -p /sw/cache/geonames/index && \
    wget -nv https://bigmech.s3.amazonaws.com/travis/geonames%2Bworedas.zip && \
    mv geonames+woredas.zip /sw/cache/geonames/index && \
    cd /sw/cache/geonames/index && \
    unzip geonames+woredas.zip
