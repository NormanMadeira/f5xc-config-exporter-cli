"""Built-in F5 XC resource catalog and known system-managed objects to skip.

The catalog is intentionally explicit and is not a complete inventory of every
F5 Distributed Cloud API resource. Review it against the API schemas used by
your tenant before relying on it for a migration or recovery workflow.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceType:
    key: str
    label: str
    collection: str
    category: str
    api_root: str = "/api/config/namespaces"
    skip_names: tuple[str, ...] = ()
    skip_reason: str = "Built-in/system-managed F5 XC object excluded by the catalog."


RESOURCE_TYPES = [
    ResourceType("http_loadbalancer", "HTTP Load Balancers", "http_loadbalancers", "Application Delivery"),
    ResourceType("tcp_loadbalancer", "TCP Load Balancers", "tcp_loadbalancers", "Application Delivery"),
    ResourceType("origin_pool", "Origin Pools", "origin_pools", "Application Delivery"),
    ResourceType("healthcheck", "Health Checks", "healthchecks", "Application Delivery"),
    ResourceType("app_firewall", "App Firewalls / WAF Policies", "app_firewalls", "Security"),
    ResourceType(
        "service_policy", "Service Policies", "service_policys", "Security",
        skip_names=("ves-io-allow-all", "ves-io-next-policy-set"),
        skip_reason="Known built-in F5 XC service policy excluded from export.",
    ),
    ResourceType("forward_proxy_policy", "Forward Proxy Policies", "forward_proxy_policys", "Security"),
    ResourceType("rate_limiter_policy", "Rate Limiter Policies", "rate_limiter_policys", "Security"),
    ResourceType(
        "malicious_user_mitigation", "Malicious User Mitigations", "malicious_user_mitigations", "Security",
        skip_names=("ves-io-default-malicious-user-mitigation",),
        skip_reason="Known built-in F5 XC default mitigation policy excluded from export.",
    ),
    ResourceType("user_identification", "User Identification Policies", "user_identifications", "Security"),
    ResourceType("ip_prefix_set", "IP Prefix Sets", "ip_prefix_sets", "Networking"),
    ResourceType("alert_policy", "Alert Policies", "alert_policys", "Monitoring"),
    ResourceType("alert_receiver", "Alert Receivers", "alert_receivers", "Monitoring"),
    ResourceType("global_log_receiver", "Global Log Receivers", "global_log_receivers", "Monitoring"),
    ResourceType("api_definition", "API Definitions", "api_definitions", "API Security"),
    ResourceType("certificate", "Certificates", "certificates", "Certificates"),
    ResourceType("certificate_chain", "Certificate Chains", "certificate_chains", "Certificates"),
    ResourceType("service_discovery", "Service Discovery", "discoverys", "Application Delivery"),
    ResourceType("report_config", "Report Configurations", "report_configs", "Monitoring", "/api/report/namespaces"),
]
