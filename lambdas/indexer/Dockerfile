FROM public.ecr.aws/lambda/python:3.13@sha256:59f2e865cbc224b18d35a393690a7e25c1e792c3fccb4cc924bf545f9470b4eb AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
