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

RUN mkdir -p /builds
RUN python3 -mvenv /builds/scriptworker
WORKDIR /builds/scriptworker/

COPY ./requirements/ /builds/scriptworker/requirements
COPY setup.py /builds/scriptworker/
COPY setup.cfg /builds/scriptworker/
COPY scriptworker/ /builds/scriptworker/scriptworker
COPY .git /builds/scriptworker/.git
COPY .coveragerc /builds/scriptworker/
COPY tox.ini /builds/scriptworker/
COPY MANIFEST.in /builds/scriptworker/
COPY version.json /builds/scriptworker/

COPY secrets.json /builds/scriptworker/
# get trusted gpg pubkeys
RUN git clone https://github.com/mozilla-releng/cot-gpg-keys.git /builds/key_repo
RUN git clone https://github.com/mozilla-releng/build-puppet.git /builds/puppet

# allow for rebuild_gpg_homedirs testing
COPY docker/gnupg.yaml /builds/dev_gnupg.yaml
COPY scriptworker/test/data/gpg/keys/scriptworker@example.com.pub /builds/dev_pubkey.asc
COPY scriptworker/test/data/gpg/keys/scriptworker@example.com.sec /builds/dev_privkey.asc

# test-specific installs
ARG DOCKER_TEST
ENV DOCKER_TEST ${DOCKER_TEST:-true}
RUN sh -c 'if [ "$DOCKER_TEST" = true ] ; then bin/pip3 install -r /builds/scriptworker/requirements/test.in && bin/python3 setup.py develop; else bin/pip3 install -r /builds/scriptworker/requirements/base.txt && bin/python3 setup.py install; fi'
