FROM public.ecr.aws/lambda/python:3.11.2025.10.22.15@sha256:fc8f9cc8bdf3c4bcb318f940e8681262ac5d174c6f3eafc7dc4b6e7a977a5e8d AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
