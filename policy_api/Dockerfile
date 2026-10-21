# Build where PyPI (or your internal mirror) is reachable, then move the image into the contained environment.
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN useradd --system --uid 10001 app && mkdir -p /data/exports /srv/.matplotlib \
    && chown app /data/exports /srv/.matplotlib
USER app

# matplotlib needs a writable cache; build its font cache now so the first chart isn't slow.
ENV MPLCONFIGDIR=/srv/.matplotlib MPLBACKEND=Agg
RUN python -c "import matplotlib.pyplot"

ENV EXPORT_DIR=/data/exports \
    PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    DO_NOT_TRACK=1

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
