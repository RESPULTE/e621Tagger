FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 STUDIO_DATA_ROOT=/data STUDIO_FRONTEND_ROOT=/app/frontend/dist
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home studio && mkdir /data && chown studio:studio /data
COPY backend/ ./backend/
COPY --from=frontend /build/dist ./frontend/dist/
USER studio
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import os,urllib.request; from urllib.parse import urlsplit; r=urllib.request.Request('http://127.0.0.1:8000/health',headers={'Host':urlsplit(os.environ['STUDIO_PUBLIC_ORIGIN']).netloc}); urllib.request.urlopen(r,timeout=4)"
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
