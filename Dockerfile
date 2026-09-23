FROM jbarlow83/ocrmypdf-ubuntu:v17.4.2

USER root
ENV DEBIAN_FRONTEND=noninteractive \
    VERAPDF_VERSION=1.30.2 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress \
      default-jre-headless curl unzip ca-certificates python3-venv poppler-utils \
    && python3 -m venv /opt/worker-venv \
    && /opt/worker-venv/bin/pip install --no-cache-dir fastapi==0.116.1 uvicorn==0.35.0 python-multipart==0.0.20 python-docx==1.2.0 weasyprint==70.0 'PyMuPDF>=1.26,<2' \
    && curl -fsSL https://software.verapdf.org/rel/verapdf-installer.zip -o /tmp/verapdf.zip \
    && unzip -q /tmp/verapdf.zip -d /tmp/verapdf-installer \
    && printf '%s\n' '<AutomatedInstallation langpack="eng"><com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/><com.izforge.izpack.panels.target.TargetPanel id="install_dir"><installpath>/opt/verapdf</installpath></com.izforge.izpack.panels.target.TargetPanel><com.izforge.izpack.panels.packs.PacksPanel id="sdk_pack_select"><pack index="0" name="veraPDF GUI" selected="true"/><pack index="1" name="veraPDF Mac and *nix Scripts" selected="true"/><pack index="2" name="veraPDF Validation model" selected="true"/><pack index="3" name="veraPDF Documentation" selected="false"/><pack index="4" name="veraPDF Sample Plugins" selected="false"/></com.izforge.izpack.panels.packs.PacksPanel><com.izforge.izpack.panels.install.InstallPanel id="install"/><com.izforge.izpack.panels.finish.FinishPanel id="finish"/></AutomatedInstallation>' > /tmp/verapdf-auto.xml \
    && java -jar $(find /tmp/verapdf-installer -name 'verapdf-izpack-installer-*.jar' | head -1) /tmp/verapdf-auto.xml \
    && rm -rf /var/lib/apt/lists/* /tmp/verapdf* \
    && (id -u app >/dev/null 2>&1 || useradd --system --uid 10001 --create-home app) \
    && mkdir -p /work && chown -R app:app /work /opt/verapdf

COPY worker.py /app/worker.py
COPY pdfua2_rebuild_worker.py /app/pdfua2_rebuild_worker.py
USER app
WORKDIR /work
EXPOSE 8080
ENTRYPOINT []
CMD ["/opt/worker-venv/bin/python", "-m", "uvicorn", "worker:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
