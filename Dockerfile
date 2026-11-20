FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
# Keys come from the host's environment: FIRMS_MAP_KEY, AIRNOW_API_KEY, WFB_ACCESS_TOKEN.
ENV PORT=8000
EXPOSE 8000
CMD ["wildfirebot-mcp", "--http"]
