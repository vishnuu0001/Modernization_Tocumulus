# StratIQ Modernization Studio

## Annual Enterprise Pricing and Unlimited-Use License

**Commercial status:** Recommended pricing proposal — non-binding until incorporated into an executed order form  
**Prepared:** 7 November 2026  
**Deployment:** Customer-managed on-premises infrastructure or customer-managed Microsoft Azure  
**Currency:** United States dollars (USD), excluding applicable taxes  
**Scope:** StratIQ Modernization Studio only

---

## 1. Executive commercial proposal

StratIQ Modernization Studio is offered as a fixed annual enterprise license. The license is not priced per user, application, repository, line of code, transformation, token, API call, environment, server, or generated artifact.

| Commercial item | Year 1 | Year 2 onward | Charging basis |
|---|---:|---:|---|
| Annual enterprise software license | $48,000 | $48,000 | Fixed annual fee; unlimited internal usage |
| Annual support and maintenance | $12,000 | $12,000 | Fixed annual fee |
| Initial deployment and enablement | $27,000 | — | One-time professional-services fee |
| **Total** | **$87,000** | **$60,000/year** | No usage overage |

The recommended recurring commercial price is therefore **$60,000 per 12-month term**. The first-year total is **$87,000**, including implementation. At unchanged pricing, the three-year contract value is **$207,000**.

This price is a newly evaluated recommendation. It does not use or extrapolate the figures in the earlier StratIQ application summary.

---

## 2. What “unlimited usage” means

During a paid 12-month license term, the licensed customer may use StratIQ Modernization Studio across its internal modernization portfolio without consumption charges or volume limits.

The annual fee includes:

- unlimited named and concurrent internal users;
- unlimited applications, projects, repositories, and source-code volume;
- unlimited analysis, planning, generation, conversion, validation, repair, comparison, and export runs;
- unlimited generated files and modernization artifacts;
- unlimited development, test, staging, disaster-recovery, and production environments controlled by the customer;
- unlimited deployments on the customer’s on-premises infrastructure and/or in the customer’s Microsoft Azure tenant;
- no per-token, per-prompt, per-job, per-application, per-machine, per-core, or per-line-of-code fee from StratIQ; and
- no overage invoice when usage increases.

“Unlimited” is a consumption entitlement, not a transfer of ownership or a right to redistribute the product. The license remains limited to the contracting legal entity and any affiliates explicitly named in the order form, for their internal business purposes. Resale, sublicensing, hosted service-bureau use for third parties, and transfer of the software remain prohibited unless separately agreed. These legal boundaries do not impose a usage-volume cap.

If the subscription is not renewed, the right to run the software ends at the conclusion of the paid term. Customer-owned source code, generated source code, reports, and exported project artifacts remain with the customer.

---

## 3. Price build-up

### 3.1 Annual enterprise license — $48,000

| License component | Annual value | What it covers |
|---|---:|---|
| Core platform and administration | $28,000 | Self-managed application, administration, project lifecycle, access control, audit history, and export |
| Governed analysis and transformation | $10,000 | Discovery, planning, prompt-to-project generation, source conversion, deterministic scaffolding, and repair workflows |
| Validation and release governance | $6,000 | Compiler/parser validation, whole-project build evidence, comparison, approvals, snapshots, checksums, and release controls |
| Unlimited portfolio entitlement | $4,000 | Removal of user, application, repository, LOC, run, environment, and machine-based metering |
| **Annual license total** | **$48,000** | Fixed for the licensed organization |

This allocation explains how the commercial price was constructed; it does not create separately metered sub-licenses.

### 3.2 Annual support and maintenance — $12,000

| Support component | Annual value | Included service |
|---|---:|---|
| Product releases and corrective maintenance | $4,000 | Supported releases, defect corrections, security fixes, and maintenance guidance |
| Technical support capacity | $4,800 | Remote L2/L3 diagnosis for supported product faults and installation issues |
| Service reviews | $1,600 | Quarterly operational and adoption review |
| Release and license administration | $1,600 | Annual entitlement renewal, supported upgrade planning, and release notices |
| **Annual support total** | **$12,000** | Standard business-hours support |

The commercial baseline assumes remote support during agreed business hours. Severity definitions, response targets, maintenance windows, support contacts, and escalation paths must be recorded in the final support schedule. Custom development and modernization project delivery are not support incidents.

### 3.3 Initial deployment and enablement — $27,000 one time

The estimate uses **30 professional-services days at $900/day**.

| Work package | Effort | Amount | Deliverable |
|---|---:|---:|---|
| Discovery and deployment design | 4 days | $3,600 | Confirmed architecture, prerequisites, security boundary, and implementation plan |
| Platform installation and configuration | 6 days | $5,400 | One customer-managed on-premises or Azure deployment |
| Identity, network, database, and security configuration | 6 days | $5,400 | Environment-specific connectivity and security configuration |
| Local model and language-toolchain configuration | 5 days | $4,500 | Approved local inference and available validators/build tools configured |
| Pilot, acceptance support, and remediation | 5 days | $4,500 | One representative pilot journey and agreed acceptance evidence |
| Administrator and operator enablement | 2 days | $1,800 | Remote knowledge-transfer sessions and operating guidance |
| Delivery management and handover documentation | 2 days | $1,800 | Runbook, configuration record, and handover |
| **Implementation total** | **30 days** | **$27,000** | Fixed baseline scope |

The fixed implementation amount assumes timely access to the target environment, one deployment topology, one identity integration, one pilot application, remote delivery, and no bespoke product development. A materially different scope requires a written change order.

---

## 4. Multi-year cost view

| Contract horizon | License and support | Initial deployment | Total contract value | Effective monthly average |
|---|---:|---:|---:|---:|
| 1 year | $60,000 | $27,000 | **$87,000** | $7,250 |
| 3 years | $180,000 | $27,000 | **$207,000** | $5,750 |
| 5 years | $300,000 | $27,000 | **$327,000** | $5,450 |

No multi-year discount is assumed. Each renewal is a new 12-month term. The order form should state any price-protection period and renewal cap; neither is implied by this document.

---

## 5. Global self-managed market benchmark

The comparison set deliberately excludes products that are available only as vendor-hosted SaaS. It includes products installable on-premises, in a customer-controlled cloud environment, or in Azure. Products differ in functional scope, so the table compares commercial structure and deployment rather than claiming feature-for-feature equivalence.

| Product | Customer-controlled deployment | Published commercial model | Pricing evidence | Relevance to StratIQ pricing |
|---|---|---|---|---|
| **CAST Imaging** | Local server, customer cloud, Windows, containers, Kubernetes, and AKS/on-premises Kubernetes | Named-application annual license; larger and portfolio plans by quotation | **$10,200/year for one application**. Technical support and updates are included. Extensions add 10% or 20%; CAST Gatekeeper adds 40%. | Best public annual benchmark. CAST’s documentation describes restrictions based on applications, contributing developers, application name/size, users, and expiry. |
| **IBM watsonx Code Assistant for Z** | Fully on-premises deployment on IBM Software Hub/OpenShift is available, including the model; substantial compute/GPU infrastructure is required | Authorized-user and virtual-server metrics for on-premises core components; transformation and other components have separate metrics | Public documentation describes the metrics and infrastructure, but the enterprise price is quotation-based. | Demonstrates that enterprise modernization platforms commonly meter users, servers, components, and infrastructure rather than offering one unrestricted portfolio fee. Its focus is primarily IBM Z modernization. |
| **OpenText Enterprise Analyzer / Modernization portfolio** | OpenText supports customer-managed on-premises and Microsoft Azure deployment options | Named-user, concurrent-user, product-specific, or enterprise-agreement terms; pricing by quotation | Public price not available. OpenText also advertises an **OpenPass Unlimited** enterprise framework with unlimited use and no certification requirement. | Validates an unlimited enterprise license as a recognized commercial model, while highlighting that large-suite pricing is normally negotiated. |
| **Ispirer Toolkit** | Runs in the customer environment on Windows, Linux, or Unix and supports on-premises, Azure, and other clouds; offline licenses are available | Project duration, migration direction, objects, LOC, data volume, and machines; larger work is custom-priced | Entry tiers are public, but full-support application conversion and larger migrations require a quotation. | Useful commercial contrast: low entry prices apply to tightly restricted volumes, while enterprise application conversion is scope-priced. |
| **TSRI JANUS Studio** | Modernization factory can operate in the customer data center or cloud | Firm-fixed project pricing and fixed-rate testing/implementation support | No public standard license price. | A project/service benchmark rather than a directly purchasable unlimited platform license. |

### Public-source normalization

CAST is the only close self-managed comparator in this set with a directly published annual software price. At its published one-application rate:

| Named applications | CAST Imaging published base price | StratIQ annual license + support | Difference |
|---:|---:|---:|---:|
| 1 | $10,200 | $60,000 | StratIQ costs $49,800 more |
| 4 | $40,800 | $60,000 | StratIQ costs $19,200 more |
| 6 | $61,200 | $60,000 | StratIQ costs $1,200 less |
| 10 | $102,000 | $60,000 | StratIQ costs $42,000 less |

The recommended StratIQ recurring price therefore reaches nominal price parity at approximately **six applications**, before any CAST extensions, Gatekeeper uplift, or negotiated portfolio discount. This is a price-model comparison only: CAST Imaging and StratIQ Modernization Studio do not provide identical functionality.

---

## 6. Pricing conclusion

### Recommended list price

- **Annual unlimited enterprise license:** $48,000
- **Annual support and maintenance:** $12,000
- **Annual recurring total:** **$60,000**
- **One-time implementation:** $27,000
- **Year 1 total:** **$87,000**

This is a defensible mid-market enterprise position because it:

1. is materially above a single-application analysis license;
2. becomes economically attractive at a portfolio of approximately six applications;
3. includes modernization workflow, generation/conversion, validation, governance, and export rather than discovery visualization alone;
4. removes the common application, LOC, seat, machine, core, and run constraints; and
5. remains below the opaque, quotation-led commercial structure typical of large enterprise modernization suites.

The annual price should not be reduced solely because the software runs on customer infrastructure. Self-managed deployment transfers compute, storage, backup, monitoring, and model-operating cost to the customer, but it also provides source-code control, deployment sovereignty, predictable software spend, and freedom from vendor-side consumption billing.

---

## 7. Included and excluded costs

### Included

- StratIQ Modernization Studio software for the paid term;
- unlimited internal consumption rights described in Section 2;
- supported software updates and corrective maintenance;
- standard remote product support;
- the first deployment and enablement package described in Section 3.3; and
- licensing for both customer-managed on-premises and customer-managed Azure use.

### Excluded

- Azure consumption, virtual machines, AKS, storage, networking, backup, monitoring, and data-transfer charges;
- on-premises servers, operating systems, GPUs, storage, backup, and operational staffing;
- third-party model, database, compiler, SDK, commercial library, or operating-system licenses;
- implementation of additional environments or identity providers beyond the baseline;
- custom product features, new target-language engines, bespoke conversion rules, or client-specific integrations;
- modernization delivery services for individual applications, data cleansing, business-rule validation, user-acceptance testing, security certification, production cutover, and managed operations;
- travel and expenses; and
- taxes, duties, and withholding.

Customer infrastructure and third-party licenses remain payable even though StratIQ usage itself is unlimited. Performance and concurrency are bounded by the capacity and configuration of the customer-managed infrastructure; this is a technical capacity constraint, not a license or billing limit.

---

## 8. Deployment and data boundary

The license supports either of the following customer-controlled patterns:

1. **On-premises:** the application, PostgreSQL data, source inputs, generated outputs, local model endpoint, and build toolchains operate in the customer’s data center.
2. **Microsoft Azure:** the same components operate in the customer’s Azure subscription on an agreed VM or container topology.
3. **Hybrid customer-controlled:** application services and data remain in customer-controlled environments, with only explicitly approved external services connected.

The StratIQ license does not require source code to be sent to a StratIQ-hosted SaaS service. Any external model, package repository, telemetry, or integration must be separately selected and approved by the customer. Fully disconnected operation depends on the customer providing mirrored dependencies, installation media, approved models, and the required language toolchains.

---

## 9. Commercial assumptions and approval points

Before issue as a binding quotation, the parties must confirm:

- contracting entity and licensed affiliates;
- country, tax treatment, invoicing currency, and payment terms;
- on-premises, Azure, or hybrid deployment topology;
- support hours, time zone, severity definitions, and response targets;
- initial implementation scope and acceptance criteria;
- third-party software and infrastructure responsibilities;
- annual renewal notice, price protection, and termination terms;
- intellectual-property treatment of generated outputs and client materials; and
- security, privacy, confidentiality, warranty, and liability terms.

This pricing document is not a statement that generated software is automatically production-ready. Client architecture review, testing, security approval, performance qualification, business acceptance, and release authorization remain required.

---

## 10. Sources

Sources were reviewed on 7 November 2026. Vendor offers and prices can change and must be revalidated during procurement.

1. [CAST Imaging pricing](https://www.castsoftware.com/imaging/pricing) — one-application annual price, included support/updates, extensions, and portfolio quotation.
2. [CAST Imaging deployment options](https://doc.castsoftware.com/imaging/install/before-you-start/deployment-options/) — Windows, containers, Kubernetes, AKS, and on-premises deployment patterns.
3. [CAST Imaging license management](https://doc.castsoftware.com/export/IMAGING/Admin%2BCenter%2B-%2BLicense%2Bmanagement%2Bpanel) — application, developer, size, user, and term-based license controls.
4. [IBM watsonx Code Assistant for Z license guide](https://www.ibm.com/docs/en/watsonx/watsonx-code-assistant-4z/2.x?topic=information-license-guide-watsonx-code-assistant-z) — deployment choices and charging metrics.
5. [IBM watsonx Code Assistant for Z infrastructure requirements](https://www.ibm.com/docs/en/watsonx/watsonx-code-assistant-4z/2.x?topic=prerequisites-infrastructure-requirements) — on-premises compute, memory, storage, and GPU requirements.
6. [OpenText deployment options](https://www.opentext.com/deployment) — customer-managed on-premises and Microsoft Azure deployment choices.
7. [OpenText OpenPass](https://www.opentext.com/openpass) — enterprise and unlimited-use commercial frameworks.
8. [Ispirer pricing](https://www.ispirer.com/pricing) — duration, volume, LOC, machine, support, online/offline, and custom enterprise pricing factors.
9. [Ispirer Toolkit overview](https://www.ispirer.com/) — customer-environment, on-premises, Azure, and hybrid deployment coverage.
10. [TSRI modernization solution](https://tsri.com/solution/) — fixed-price modernization approach and JANUS Studio positioning.

