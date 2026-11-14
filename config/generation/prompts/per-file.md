# Governed per-file contract v1

The response must contain exactly these two sections:

<generated_file>
the complete raw file content
</generated_file>
<generation_notes>
{"applied_rule_ids":[],"not_applicable_rules":[{"id":"","reason":""}],
"conflicts_resolved":[],"assumptions":[],"manifest_ids_covered":[],
"manifest_coverage":[{"id":"IN-001","actions":["capture","validate","persist","audit"]}],
"self_check":{"complete":true,"imports_resolve":true,"contracts_match":true,
"version_apis_verified":true,"security_checked":true,"tests_addressed":true}}
</generation_notes>

Do not put Markdown fences around either section. Every rule applicable to the file must
appear in applied_rule_ids. Every supplied rule not applicable to the file must have a
specific reason. Never claim an IN/OUT ID that the file does not capture, validate,
persist, audit, produce, transmit, test, or render. For every claimed ID, list the exact
actions performed in manifest_coverage. Allowed actions are capture, validate, persist,
audit, produce, render, transmit, test, error-state, implement, authorize, and configure.
Use only those canonical action names. For CI/CD evidence use implement for build,
configure for deployment, transmit for publishing, and validate for security scanning.
