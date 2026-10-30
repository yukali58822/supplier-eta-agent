ARG ERPNEXT_VERSION=v15.38.0
FROM frappe/erpnext:${ERPNEXT_VERSION}
USER frappe
WORKDIR /home/frappe/frappe-bench
COPY --chown=frappe:frappe erpnext_app/supplier_eta apps/supplier_eta
RUN ./env/bin/pip install --no-cache-dir -e apps/supplier_eta
