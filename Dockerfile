FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8765
# /data  : WAV files (read-only is fine)
# /state : accounts, labels, audit log (beeplabel.db) - mount a volume so it persists
VOLUME ["/data", "/state"]
CMD ["beeplabel", "--data-dir", "/state", "serve", "--dataset", "/data", "--host", "0.0.0.0", "--port", "8765", "--no-browser"]
