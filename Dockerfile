#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

######################################################################
# Node stage to deal with static asset construction
######################################################################
ARG PY_VER=3.10-slim-bookworm

# if BUILDPLATFORM is null, set it to 'amd64' (or leave as is otherwise).
ARG BUILDPLATFORM=${BUILDPLATFORM:-amd64}
FROM --platform=${BUILDPLATFORM} node:18-bullseye-slim AS superset-node

ARG NPM_BUILD_CMD="build"

RUN apt-get update -qq \
    && apt-get install -yqq --no-install-recommends \
        build-essential \
        python3

ENV BUILD_CMD=${NPM_BUILD_CMD} \
    PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
# NPM ci first, as to NOT invalidate previous steps except for when package.json changes
WORKDIR /app/superset-frontend

RUN --mount=type=bind,target=/frontend-mem-nag.sh,src=./docker/frontend-mem-nag.sh \
    /frontend-mem-nag.sh

RUN --mount=type=bind,target=./package.json,src=./superset-frontend/package.json \
    --mount=type=bind,target=./package-lock.json,src=./superset-frontend/package-lock.json \
    npm ci

COPY ./superset-frontend ./
# This seems to be the most expensive step
RUN npm run ${BUILD_CMD}

# Compile translations for the frontend
WORKDIR /app
COPY superset/translations ./superset/translations
COPY ./scripts/translations/po2json.sh ./scripts/translations/po2json.sh
RUN ./scripts/translations/po2json.sh

######################################################################
# Final lean image...
######################################################################
FROM python:${PY_VER} AS lean

WORKDIR /app
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    SUPERSET_ENV=production \
    FLASK_APP="superset.app:create_app()" \
    PYTHONPATH="/app/pythonpath" \
    SUPERSET_HOME="/app/superset_home" \
    SUPERSET_PORT=8088

RUN mkdir -p ${PYTHONPATH} superset/static requirements superset-frontend apache_superset.egg-info requirements \
    && useradd --user-group -d ${SUPERSET_HOME} -m --no-log-init --shell /bin/bash superset \
    && apt-get update -qq && apt-get install -yqq --no-install-recommends \
        build-essential \
        curl \
        default-libmysqlclient-dev \
        libsasl2-dev \
        libsasl2-modules-gssapi-mit \
        libpq-dev \
        libecpg-dev \
        libldap2-dev \
    && touch superset/static/version_info.json \
    && chown -R superset:superset ./* \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=superset:superset pyproject.toml setup.py MANIFEST.in README.md ./
# setup.py uses the version information in package.json
COPY --chown=superset:superset superset-frontend/package.json superset-frontend/
COPY --chown=superset:superset requirements/base.txt requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade setuptools pip && \
    pip install -r requirements/base.txt

# Copy the compiled frontend assets
COPY --chown=superset:superset --from=superset-node /app/superset/static/assets superset/static/assets

# Copy the compiled translations for the frontend
COPY --chown=superset:superset --from=superset-node /app/superset/superset/translations/*/LC_MESSAGES/*.json superset/static/translations

## Lastly, let's install superset itself
COPY --chown=superset:superset superset superset
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e .

# Compile translations for the backend - this generates .mo files
COPY ./scripts/translations/generate_po_files.sh ./scripts/translations/
RUN ./scripts/translations/generate_po_files.sh && \
    chown -R superset:superset superset/translations

COPY --chmod=755 ./docker/run-server.sh /usr/bin/
USER superset

HEALTHCHECK CMD curl -f "http://localhost:${SUPERSET_PORT}/health"

EXPOSE ${SUPERSET_PORT}

CMD ["/usr/bin/run-server.sh"]

######################################################################
# Dev image...
######################################################################
FROM lean AS dev
ARG GECKODRIVER_VERSION=v0.33.0 \
    FIREFOX_VERSION=117.0.1

USER root

RUN apt-get update -qq \
    && apt-get install -yqq --no-install-recommends \
        libnss3 \
        libdbus-glib-1-2 \
        libgtk-3-0 \
        libx11-xcb1 \
        libasound2 \
        libxtst6 \
        wget \
        git \
        pkg-config

RUN pip install playwright
RUN playwright install-deps
RUN playwright install chromium

# Install GeckoDriver WebDriver
RUN wget -q https://github.com/mozilla/geckodriver/releases/download/${GECKODRIVER_VERSION}/geckodriver-${GECKODRIVER_VERSION}-linux64.tar.gz -O - | tar xfz - -C /usr/local/bin \
    # Install Firefox
    && wget -q https://download-installer.cdn.mozilla.net/pub/firefox/releases/${FIREFOX_VERSION}/linux-x86_64/en-US/firefox-${FIREFOX_VERSION}.tar.bz2 -O - | tar xfj - -C /opt \
    && ln -s /opt/firefox/firefox /usr/local/bin/firefox \
    && apt-get autoremove -yqq --purge wget && rm -rf /var/[log,tmp]/* /tmp/* /var/lib/apt/lists/*
# Cache everything for dev purposes...

COPY --chown=superset:superset requirements/development.txt requirements/
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements/development.txt

USER superset
######################################################################
# CI image...
######################################################################
FROM lean AS ci

COPY --chown=superset:superset --chmod=755 ./docker/*.sh /app/docker/

CMD ["/app/docker/docker-ci.sh"]
