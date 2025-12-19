FROM public.ecr.aws/lambda/python:3.11.2025.12.18.13@sha256:884a202a594bedfbfebc5c48f306ac457dc77011bc320e3603a1e47b431a276c AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
