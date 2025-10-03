FROM public.ecr.aws/lambda/python:3.13.2025.10.02.15@sha256:bb271cdf756c699fd653bfbd26b1dbb8de031d119d1c109ac5668647206b1db4 AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
