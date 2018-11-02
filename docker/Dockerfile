ARG PY_DOT_VERSION
FROM python:${PY_DOT_VERSION:-3.7}

# gnupg 2.0.x
RUN echo "deb http://http.us.debian.org/debian/ oldstable non-free contrib main" >> /etc/apt/sources.list

RUN apt-get update && apt-get install -y --allow-downgrades \
    git \
    gnupg2=2.0.26-6+deb8u2 \
    gnupg-agent=2.0.26-6+deb8u2 \
&& rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/gpg2 /usr/bin/gpg

RUN mkdir -p /builds/src
RUN mkdir -p /builds/dev
RUN python3 -mvenv /builds/scriptworker
WORKDIR /builds/scriptworker/

COPY ./requirements/ /builds/src/requirements
COPY setup.py /builds/src/
COPY setup.cfg /builds/src/
COPY scriptworker/ /builds/src/scriptworker
COPY .git /builds/src/.git
COPY .coveragerc /builds/src/
COPY tox.ini /builds/src/
COPY MANIFEST.in /builds/src/
COPY version.json /builds/src/

COPY secrets.json /builds/dev/
# get trusted gpg pubkeys
RUN git clone https://github.com/mozilla-releng/cot-gpg-keys.git /builds/key_repo
RUN git clone https://github.com/mozilla-releng/build-puppet.git /builds/puppet

# allow for rebuild_gpg_homedirs testing
COPY docker/gnupg.yaml /builds/dev/gnupg.yaml
COPY scriptworker/test/data/gpg/keys/scriptworker@example.com.pub /builds/dev/pubkey.asc
COPY scriptworker/test/data/gpg/keys/scriptworker@example.com.sec /builds/dev/privkey.asc

# test-specific installs
ARG DOCKER_TEST
ENV DOCKER_TEST ${DOCKER_TEST:-true}
RUN sh -c 'if [ "$DOCKER_TEST" = true ] ; then bin/pip3 install -r /builds/src/requirements/test.in && (cd /builds/src && /builds/scriptworker/bin/python3 setup.py develop); else bin/pip3 install -r /builds/src/requirements/base.txt && (cd /builds/src && /builds/scriptworker/bin/python3 setup.py install); fi'

# Entrypoint
# CMD
