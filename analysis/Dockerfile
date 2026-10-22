# YourAnalyst analysis API: indicators.py + api.py only (the bist100 ML engine is not needed here).
FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pandas==2.2.3 numpy==2.1.3
COPY indicators.py api.py test_indicators.py ./
RUN python -m unittest -q test_indicators
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"
CMD ["python", "-u", "api.py"]
