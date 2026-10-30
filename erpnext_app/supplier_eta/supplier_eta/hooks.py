app_name = "supplier_eta"
app_title = "Supplier ETA"
app_publisher = "Supplier ETA Agent"
app_description = "供應商交期確認 Agent 的 ERPNext 端"
app_email = "admin@example.com"
app_license = "MIT"
required_apps = ["erpnext"]

after_install = "supplier_eta.setup.install.after_install"
after_migrate = "supplier_eta.setup.install.after_install"

doc_events = {
    "Supplier ETA Case": {
        "on_update": "supplier_eta.api.cases.on_case_update",
    }
}
