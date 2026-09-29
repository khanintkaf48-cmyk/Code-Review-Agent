FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY .reviewer/standards.md /data/standards.md
ENV REVIEWER_HOME=/data
VOLUME /data
EXPOSE 8080
# 0.0.0.0 inside the container; ALWAYS set REVIEWER_DASHBOARD_TOKEN when exposing it
CMD ["reviewer", "serve", "--host", "0.0.0.0", "--port", "8080"]
