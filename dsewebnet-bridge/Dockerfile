FROM python:3.11-alpine

ENV PYTHONUNBUFFERED=1

# Pinned on purpose: the bridge uses the paho-mqtt 2.x callback API.
RUN pip install --no-cache-dir \
      "aiohttp>=3.9,<4" \
      "yarl>=1.9,<2" \
      "paho-mqtt>=2.1,<3"

COPY run.sh /run.sh
COPY dsewebnet-bridge.py /dsewebnet-bridge.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
