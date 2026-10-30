# Research: How Data/BI Teams Operate + Selling VSC Data as a Product

_Research spike, 2026-10-22. Uses public sources only. This is not legal advice._

## 1. Team roles (who owns what)

| Role | Owns | In this repo |
|---|---|---|
| Director of Data | Roadmap, budget, data-product P&L, privacy sign-off | `docs/` roadmap, ADRs |
| Analytics Manager | Intake triage, priorities, stakeholder SLAs | Issue templates, backlog |
| Data Engineer | Ingestion (TPA admin feed to raw), orchestration, SLAs on freshness | `ingest/`, scheduler |
| Analytics Engineer | Staging to marts (dbt-style), tests, **metric definitions** | `models/`, `metrics.yml` |
| BI/Report Developer | Dashboards, refresh, row-level security | `dashboards/` |
| Actuary / Data Scientist | Loss ratio, IBNR/earned-premium logic, loss-ratio drivers | `analysis/` notebooks |
| Financial Analyst | Reconciles reported numbers to the GL (premium, claims paid, reserves) | Reconciliation tests |
| Front-end/UX | Stakeholder-facing app, accessibility | Web app |

Use a data-product model. Every dataset has a **product owner**, a **producer** and **consumers**, and each consumer provides acceptance criteria ([petronellatech](https://petronellatech.com/blog/data-contracts-the-new-sla-for-reliable-ai-analytics-crm/)). Write SLAs together with the teams that consume the data ([Bigeye](https://www.bigeye.com/blog/the-complete-guide-to-understanding-data-slas)).

## 2. How they work with stakeholders

- **Intake:** a single request form that asks for the business question, the decision it drives, the audience, the deadline, and how the result will be judged. Triage weekly and prioritize by value and effort.
- **SLAs/SLOs:** make every target measurable, for example "loss-ratio mart refreshed by 7:00 AM CT daily, 99.5% of days" ([dbt Labs](https://www.getdbt.com/blog/data-slas-best-practices)).
- **Data contracts** between ingest and the warehouse cover schema, nullability, allowed ranges, uniqueness and freshness. Breaking changes need versioning ([decube](https://decube.io/post/master-data-contracts-key-practices-for-data-engineers-success)).
- **Metric definitions** live in one place (semantic layer or YAML): `loss_ratio = incurred_claims / earned_premium`, plus earned-premium method, cancellation handling, and the as-of date. The dashboard never computes its own version.
- **Documentation:** a data dictionary, lineage, a README for each mart, and a "how to read this report" page.
- **Release process:** dev, CI (tests, contract checks), staging, prod. Changelog and stakeholder release notes. Freshness and test failures send alerts.

## 3. Selling aggregated VSC/warranty reports (data broker)

**What buyers want**
- **Dealers / dealer reinsurance (PORC/dealer-owned company) owners:** their loss ratio against peers, claim frequency and severity, early claims, cancellations, product mix. Benchmarks cluster around a ~70% loss-ratio target, but this varies by brand, region and product ([CBT News](https://www.cbtnews.com/dealers-to-reassess-profit-as-reinsurance-evolve/); metric list from [EasyCare](https://info.easycare.com/hubfs/EasyCare/EasyCare%20Resource%20Hub/EC_RH_Reinsurance%20Benchmarking_1042165_040122.pdf)).
- **OEMs:** component failure rates by make, model, year and mileage band.
- **Insurers and reinsurers:** loss development triangles, rate adequacy, and how loss ratios vary by administrator, state or term.
- No public industry-wide VSC benchmark dataset turned up in these searches, which suggests a gap in the market.

**Anonymization and aggregation**
- Never sell rows at the contract or VIN level. Publish aggregates only.
- **Minimum cell size:** suppress any cell under **11** contracts, and also any cell from which a small value could be worked out (complementary suppression), following the [CMS Cell Suppression Policy](https://resdac.org/node/1506). Also require **at least 5 contributing dealers in each peer group, with no single dealer above about 25% of the cell**, so a competitor's numbers cannot be reverse-engineered.
- Remove direct identifiers (name, address, VIN, phone) at the staging step. Synthetic data is used for the demo version.

**Legal risk (have counsel verify)**
- **GLBA:** VSC finance data at dealers is typically treated as nonpublic personal information (NPI). Auto dealers fall under the FTC Safeguards Rule. Treat the source data as GLBA data.
- **State privacy laws:** all 19 general state privacy laws exempt GLBA *data*, but the exemptions apply to data, not to whole companies ([EPIC](https://epic.org/documents/unbridled-and-underregulated-removing-fcra-and-glba-exemptions-from-privacy-laws-to-hold-data-brokers-accountable/); [TrueVault](https://knowledge.truevault.com/article/227-exemptions-hipaa-glba-and-fcra)). Properly de-identified aggregates usually fall outside "personal information". CCPA-style de-identification also requires a public commitment and contractual bans on re-identification.
- **FCRA:** the risk comes from selling information about *individual consumers* that is used for eligibility decisions. Aggregates about dealers or products avoid this. The CFPB withdrew its proposed data-broker rule in 2025 ([Orrick](https://infobytes.orrick.com/2025-05-16/cfpb-withdraws-its-proposed-rule-on-data-brokers/)), but the rule could come back.
- **Contract rights and antitrust:** you need data-use and resale rights from the administrator or dealers. Sharing pricing benchmarks between competitors should be lagged and aggregated.
- **Data-broker registration** (CA, TX, OR, VT) applies only if you sell personal data. Aggregate-only sales avoid it.

**Licensing models:** a tiered subscription (dealer self-benchmark, then regional, then national), per-seat dashboard access, an annual enterprise licence or API for reinsurers and OEMs, and one-off custom studies. Use a data licence agreement that bans re-identification and resale.

## RECOMMENDATION

1. Build the project as a **one-person "team"**: each role from section 1 maps to a folder or artifact, so the whole operating model is covered.
2. Use **synthetic data only**. No real company records go anywhere.
3. Ship a `metrics.yml` contract, a data contract on the raw feed, freshness SLOs (daily 7 AM refresh), and tests in CI.
4. Add a **`publish` layer** that enforces k ≥ 11 contracts, at least 5 dealers per cell and complementary suppression. This is the step that makes a "data broker" product credible.
5. Treat commercialization as a pitch document, not a launch. List the legal gates (GLBA, data rights, counsel review) explicitly.

**Tradeoffs:** stricter suppression gives more privacy but lets buyers drill down less. dbt plus a semantic layer means more setup but one version of the truth. Daily batch is cheaper and fits VSC data, which changes slowly, while real-time adds cost with little value.
