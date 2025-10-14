FROM public.ecr.aws/lambda/python:3.11.2025.10.14.14@sha256:0807b6fe62ae36da5dc43921de2e57c24728b90ea04d3284a96baf9c0a4011b5 AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
