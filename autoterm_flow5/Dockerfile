ARG BUILD_FROM
FROM ${BUILD_FROM}

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

WORKDIR /app
COPY autoterm_flow5 /app/autoterm_flow5
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
