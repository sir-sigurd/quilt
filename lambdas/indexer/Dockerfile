FROM public.ecr.aws/lambda/python:3.11.2025.10.14.12@sha256:2ac621a78555d53ec7c127615e82912009ac0ef8fcd94bdbafc4ca00185b16a3 AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
