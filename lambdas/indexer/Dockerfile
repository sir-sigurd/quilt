FROM public.ecr.aws/lambda/python:3.11.2025.10.29.20@sha256:660e15adffd02bf04869c93f50133ee304dcdf0a798d6da33807eb47deba17df AS base

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY . /src
WORKDIR /src
RUN uv pip install --target /target -r ./requirements.txt .

FROM base
COPY --from=build /target ${LAMBDA_TASK_ROOT}

CMD [ "index.handler" ]
