# D4 — DSC/PAdES compliance and certificate procurement

**Status:** decision recorded, implementation gated. **Date:** 19 October 2026.

The client selected **certificate-backed DSC/PAdES** for purchase-order signatures,
superseding the earlier application-only Ed25519 design. No D4 code should be
written until the legal/signing model and certificate procurement plan below are
confirmed. This memo is an engineering/compliance brief, not legal advice;
Indian counsel must sign off the final model.

## 1. Executive summary

- India’s **Information Technology Act, 2000** gives legal recognition to
  electronic records and electronic signatures, and prevents a contract from
  being treated as unenforceable only because it was formed electronically
  (ss. 4, 5, 10A). A digital signature using a licensed CA’s certificate also
  attracts the “secure electronic signature” rules when the private key is under
  the signatory’s exclusive control (ss. 14–15, 42).
- The **Digital Signature (End Entity) Rules, 2015** expressly list **PAdES with
  CAdES** as an approved long-term signature format, together with RFC 3161
  timestamps, PKCS#7/CMS, SHA-2, RSA 2048–4096 and ECC P-256/P-384/P-521.
  PAdES is therefore the correct certificate-backed format for our PDF POs.
- A PAdES signature proves integrity and signer identity; it does **not** replace
  the separate certificate required under **s. 63(4) of the Bharatiya Sakshya
  Adhiniyam, 2023** when an electronic record is produced in court. We need an
  evidence-pack and certificate process in addition to the signature.
- The biggest design decision is **who signs**. A Class 3 DSC belongs to a named
  individual and its private key must remain in that person’s hardware token.
  CCA’s FAQ says an organisational “document signer” certificate is for
  automated organisational responses and is **not a replacement for the
  authorised signatory’s signature**. Server-side HSM signing of an individual’s
  Class 3 key is not a drop-in equivalent; it needs explicit CA/legal clearance.
- Recommendation: **individual Class 3 signing DSCs with document-signing EKU,
  held on FIPS 140-3 Level 2+ USB tokens, used through a local PKCS#11 agent**
  for authorised signatories. Offer CCA-licensed **eSign** as the remote/mobile
  alternative where the client accepts it. Use an organisational document-signer
  certificate only as an additional seal, not as the sole approval signature.
- Procurement is not just “buy a DSC”: it needs a CA that issues the right
  certificate profile, a CCA-licensed RFC 3161 timestamp service, OCSP/CRL
  evidence, FIPS 140-3 tokens, and an integration/UAT phase.

## 2. Legal framework

| Instrument | What it establishes | Consequence for D4 |
|---|---|---|
| IT Act 2000, s. 4 | Legal recognition of electronic records. | A PO PDF is a legally recognised record. |
| IT Act 2000, s. 5 | Where a law requires a signature, an electronic signature in the prescribed manner satisfies it. | A compliant electronic signature can stand in for a handwritten PO signature. |
| IT Act 2000, s. 10A | Contracts formed through electronic means are not unenforceable solely because of the electronic form. | Vendor acceptance of an electronically issued PO is not defeated merely by the medium. |
| IT Act 2000, s. 3A | An electronic signature is reliable if creation data is linked to the signatory, under their control, and alterations are detectable. | The private key must be exclusive to the signer; no shared tokens or server-held copies. |
| IT Act 2000, ss. 14–15 | Secure electronic record/signature where a security procedure is applied and the private key is under exclusive control. | Use a CA-issued DSC, a tamper-evident PDF signature, and a trusted timestamp. |
| IT Act 2000, s. 42 | Subscriber must protect the private key and report compromise without delay. | Token custody, PIN policy, loss/compromise and exit procedures are part of the feature. |
| Digital Signature (End Entity) Rules, 2015 (GSR 660(E)) | Prescribes signature creation/verification, certificate chain/CRL/OCSP, SHA-2, PKCS#7/CMS, and **PAdES with CAdES** for long-term signatures; RFC 3161 timestamps. | PAdES is explicitly supported. Long-term validity requires timestamping and revocation evidence. |
| CCA Interoperability Guidelines for DSCs, v4.0 (10 Feb 2026) | Certificate profiles: RSA 2048/P-256, key usage `digitalSignature`, optional `nonRepudiation`, document-signing EKUs, CRL/OCSP endpoints. | Require the CA to issue a profile suitable for PDF document signing, not just TLS/client-auth. |
| CCA Security Requirements for Crypto Devices, v2.4 (14 Aug 2026) | Class 2/3 subscriber keys in FIPS 140-2/3 Level 2+ hardware; FIPS 140-2-based new issuance discontinued after 21 Sep 2026 (subject to stated exceptions). | Procure **FIPS 140-3** validated tokens; verify OEM/model/version and non-exportable keys. |
| CCA FAQ (accessed 19 Oct 2026) | Class 3 verification and hardware key rules; document signer certificate is not a substitute for the authorised signatory; signatures verified at signing time; long-term archival recommends PAdES/CAdES/XAdES; CA timestamps use NPLI time. | Defines the signing model and evidence requirements. |
| Bharatiya Sakshya Adhiniyam, 2023, s. 63(4) | Electronic records produced in proceedings need a certificate identifying the record, device, and s. 63(2) conditions, signed by the person in charge and an expert. | Build an evidence pack and certificate template; a valid PAdES signature alone is not the statutory certificate. |

**Is a PO legally required to be signed?** Generally, Indian law does not
prescribe a DSC for ordinary commercial purchase orders. The requirement here is
a client control/contractual choice. That means we can implement a robust
signature without overstating a statutory mandate, but the chosen model must
still satisfy the IT Act if the client wants the signature to carry legal weight.

## 3. What PAdES proves — and what it does not

PAdES (ETSI EN 319 142-1) embeds a CAdES/PKCS#7 signature in a PDF and binds it
to the exact byte range of the document. A valid signature proves:

- the signed bytes have not changed since signing;
- the signature was made with the private key corresponding to the certificate;
- the certificate chains to a trusted CA (for India, the CCA/RCAI hierarchy);
- the signing time can be evidenced by an RFC 3161 timestamp;
- revocation status at signing time can be preserved with OCSP/CRL evidence.

It does **not** by itself prove:

- that the signer had authority under the company’s delegation matrix;
- that the vendor accepted the PO;
- that the record satisfies the BSA s. 63(4) certificate requirement;
- that the signer’s key was not compromised before revocation (that is a factual
  and legal question);
- that a PDF viewer trusts the Indian root by default (verification must use the
  CCA chain explicitly).

Use **PAdES B-LT** (long-term validation: signature timestamp, full certificate
chain, revocation values) or **B-LTA** (plus document/archival timestamps) for
POs that must remain verifiable for years. B-B is not enough for a legal archive.

## 4. Signing-model options

| Model | How it works | Legal/operational assessment |
|---|---|---|
| **A. Individual Class 3 DSC on a USB token** (recommended) | Each authorised signatory holds their own token; the web app invokes a local PKCS#11 agent/browser extension; the private key never leaves the token. | Strongest fit with IT Act exclusive-control and CCA subscriber custody rules. Desktop-oriented; tokens can be lost; requires an agent and user training. |
| **B. CCA-licensed eSign (Aadhaar/OTP)** | Signer authenticates with the eSign service; the ESP returns a PKCS#7/CMS signature that we embed as PAdES. | Legally recognised electronic signature, no token, works remotely/mobile. Per-signature cost, consent/logging obligations, dependence on ESP availability. The client chose DSC, so treat as an approved alternative, not a silent substitution. |
| **C. Organisational document-signer certificate + HSM** | A certificate issued for automated organisational responses signs POs server-side. | CCA’s FAQ says this is **not** a replacement for the authorised signatory’s signature. It may be useful as an additional organisational seal, but should not be the sole approval. HSM custody of Class 3 subscriber keys needs CA/legal confirmation. |

**Recommendation:** A for named approvers, with B available for remote signers if
legal/counterparty acceptance allows. Do not implement C as the primary signature.

## 5. Certificate profile requirements

Ask each CA to confirm in writing that the certificate is suitable for PDF/PAdES
document signing and matches the CCA Interoperability Guidelines:

- **Class 3** individual signing certificate (the appropriate assurance level;
  confirm current availability and naming with the CA).
- Subject includes the signer’s legal name, organisation, organisational unit
  and office location; serial number uses the CCA-prescribed PAN hash where
  applicable.
- **Key usage:** `digitalSignature`; request `nonRepudiation` as well.
- **Extended key usage:** at least one document-signing EKU accepted by the
  verifier; request **MSFT Document Signing (1.3.6.1.4.1.311.10.3.12)** and
  **Adobe Document Signing (1.2.840.113583.1.1.5)** in addition to any
  email-protection EKU. Confirm the final profile with the CA and test it in
  Adobe Acrobat/Reader and our verifier.
- **Key:** RSA 2048–4096 with SHA-256, or ECDSA P-256 with SHA-256.
- **Hardware:** FIPS 140-3 Level 2 or higher validated USB token; private key
  non-exportable; PIN lockout; tamper detection/zeroization; OEM model and
  firmware verifiable against the validation certificate.
- **Revocation/identity:** CRL distribution point and OCSP/AIA endpoints; full
  issuer chain to the CCA root; certificate policy OID and explicit text.
- **Validity:** typically one to three years; confirm with the CA and align
  renewal alerts at 90/60/30 days.
- **Timestamping:** CCA-licensed RFC 3161 TSA, SHA-256, NPLI-synchronised time;
  include the TSA certificate chain and timestamp token in the evidence pack.
- **Root trust:** ship the CCA/RCAI root and intermediate certificates; verify
  out-of-band thumbprints; do not rely on the desktop’s default trust store.

## 6. Procurement plan

### 6.1 Decisions before RFQ

1. **Signer model:** named individual DSCs, eSign, or a combination.
2. **Authority matrix:** who may sign which PO value/category, delegation and
   multi-signature rules.
3. **Documents in scope:** PO only, or PO amendments, approvals, debit notes,
   vendor acknowledgements.
4. **Counterparty position:** vendor terms acknowledging electronic POs and how
   vendors verify them.
5. **Retention period:** legal/tax/contract needs (see §8), and whether B-LT or
   B-LTA is required.
6. **Remote/mobile signers:** whether eSign is acceptable.
7. **Budget owner** for certificates, tokens, timestamps and integration.

### 6.2 CA and service selection

Invite at least three CCA-licensed CAs to quote. The current CCA list includes
Safescrypt (Sify), eMudhra, Capricorn, (n)Code Solutions, Verasys/vSign, Protean,
IDRBT, CDAC and others. Evaluate:

- Class 3 document-signing certificate profile and sample certificate;
- FIPS 140-3 token model, availability, warranty/replacement, lead time;
- RFC 3161 timestamp service, OCSP/CRL availability and SLA;
- support for our signing architecture (local PKCS#11 agent or eSign API);
- India-based support, data residency, incident/revocation process;
- price per certificate/token, timestamp volume, replacement and renewal;
- ability to provide the full certificate chain and revocation evidence.

Do not select on price alone. A cheap certificate with the wrong EKU or a
FIPS 140-2 token issued after the cut-off is a compliance defect.

### 6.3 Application documents (per signer)

Company registration/incorporation proof, GST/PAN, board resolution or
authorisation letter naming the signatory and limits, signer identity and address
proof, photograph, mobile/email, and the CA’s video/KYC process. The CA may
require physical appearance or video verification for Class 3. Allow 3–10
working days after a clean application.

### 6.4 Timestamp and evidence services

Contract a CCA-licensed TSA for RFC 3161 timestamps; confirm NPLI time source,
SHA-256 support, certificate chain, availability, and per-timestamp pricing.
Store OCSP responses (preferred) or CRLs with each signature; CRLs can be large,
so OCSP is the practical long-term evidence.

### 6.5 Indicative sequence and effort

| Step | Owner | Indicative duration |
|---|---|---|
| Legal/counterparty model sign-off | Client + counsel | 1–3 weeks |
| RFQ, CA selection, certificate profile validation | Procurement + IT | 2–3 weeks |
| Signer KYC and token issuance | CA + HR/IT | 1–2 weeks after documents |
| PAdES/agent integration and verification service | Engineering | 4–6 weeks |
| Security/UAT (tamper, expiry, revocation, LTV, Adobe/Foxit) | QA + Security | 2–3 weeks |
| Pilot with 2–3 signers, then rollout | IT + business | 2 weeks |

Do not quote a final budget until CA quotes are in. Budget lines: per-signer
certificate/token, replacement tokens, TSA volume, integration engineering,
legal review, and ongoing certificate lifecycle administration.

## 7. Implementation shape (after approval)

1. **Freeze the document.** Generate the final PO PDF; no further writes. Compute
   SHA-256 and display the exact PDF to the signer before signing.
2. **Sign locally.** The browser invokes a local agent over a restricted channel;
   the agent uses PKCS#11 with the token, shows the PIN prompt, and returns a
   detached CMS/PKCS#7 signature. The server never receives the private key or
   PIN.
3. **Embed PAdES.** Use a vetted library (for example pyHanko, or iText/PDFBox
   with a commercial licence) to embed the CMS signature as
   `/SubFilter /ETSI.CAdES.detached`, with a visible signature field showing
   signer name, designation, reason, location and time.
4. **Timestamp and add LTV.** Obtain an RFC 3161 timestamp over the signature;
   add certificate chain and OCSP/CRL values to the PDF DSS; add a document
   timestamp for B-LTA if required.
5. **Verify before storing.** Verify the byte range, CMS signature, chain to the
   CCA root, EKU/key usage, certificate validity at signing time, revocation
   status, timestamp token and LTV data. Store the verification report.
6. **Record evidence.** Persist signer user id, certificate serial and SHA-256
   thumbprint, issuer, signing time, timestamp token, document hash, signature
   revision, IP/device and audit event. The existing append-only audit log is the
   right place for the event; the signed PDF and evidence pack belong in
   immutable/WORM-capable storage.
7. **Verify publicly.** A QR code links to a public verification page showing PO
   number, signer name/designation, signing time, document hash and signature
   status. Do not expose prices, addresses or other commercial/personal data.
8. **Amendments.** Never modify signed bytes. Create a new PO revision, keep the
   old signed PDF, and require a new signature on the amended revision. The D1
   content-change seam must supersede the prior signature, not silently reuse it.
9. **Multiple signers.** Use sequential incremental signatures (each preserves
   the prior revision) or parallel signatures where the approval matrix allows;
   the End Entity Rules recognise counter/parallel signatures.
10. **Revocation/expiry.** Check revocation at signing and verification. A
    signature made while the certificate was valid remains verifiable after
    expiry if the timestamp/evidence is preserved; do not “re-sign” merely
    because the certificate expired.
11. **eSign fallback.** If approved, redirect the signer to the CCA-licensed ESP,
    capture consent and logs, receive the signed CMS, then run the same embed,
    timestamp, verify and evidence steps.

## 8. Retention, evidence and data protection

- **Retention:** contract limitation is generally three years, but tax, company,
  labour and product-liability needs can be longer. Set a retention period with
  counsel (commonly 8–10 years for commercial/tax records), and keep the signed
  PDF plus certificate chain, timestamp tokens and revocation evidence for the
  whole period.
- **BSA s. 63(4):** prepare a certificate template identifying the electronic
  record, the system that produced it, the device particulars and the s. 63(2)
  conditions, signed by the person in charge and an expert. Store the system
  description, audit logs and hash so this can be produced at each court
  submission. A PAdES signature is evidence of integrity, not the statutory
  certificate.
- **DPDP Act 2023:** signer name, PAN hash, certificate serial, IP/device and
  audit logs are personal data. Limit purpose to contract/legal obligations,
  publish a privacy notice, restrict access, set retention, use processor terms
  with the CA/TSA, and prefer India-based services. Do not put personal data in
  the public QR verification response.
- **GST/e-invoice:** a PO is not a GST e-invoice. The IRP’s signature/QR on an
  e-invoice is a separate statutory flow; do not reuse the PO signature as an
  e-invoice signature or vice versa.
- **Companies Act/MCA:** statutory filings have their own DSC requirements. A
  signatory may use the same Class 3 identity, but the PO feature must not claim
  to satisfy MCA filing requirements.
- **Cross-border:** a foreign CA’s certificate is legally valid in India only if
  recognised by CCA. Use CCA-licensed CAs for Indian signatories; vendor-side
  foreign signatures are a separate question.

## 9. Controls to build with the feature

- **Authority matrix** tied to user roles, PO value/category and delegation;
  prevent self-approval and enforce segregation of duties.
- **Token custody policy:** one named person per token, no sharing, PIN
  complexity/lockout, lost-token and compromise procedure, return/revocation on
  exit. CCA’s FAQ says an employee’s DSC must be revoked and keys destroyed when
  they leave; the organisation cannot retain it.
- **Certificate inventory:** holder, CA, serial, thumbprint, class, EKU, issue
  and expiry dates, token serial, status, renewal owner; alerts at 90/60/30 days.
- **Revocation and incident response:** notify the CA immediately on compromise,
  preserve evidence, assess signatures made in the window, and document the
  decision.
- **Immutable audit:** signing, verification, download, revocation check and
  amendment events; no update/delete on the audit trail (already enforced by the
  database triggers).
- **Vendor communication:** state that POs are electronically signed, include
  verification instructions/QR, and add a contract clause accepting electronic
  issuance where the counterparty agrees.
- **Testing:** tamper a signed byte and confirm failure; expire/revoke a test
  certificate and confirm the verifier reports “valid at signing time” from LTV;
  test Adobe Acrobat/Reader, Foxit and the server verifier; test lost-token and
  reissue; test multi-signature and amendment.

## 10. Open decisions for the client and counsel

1. **Signer model:** individual Class 3 DSC per signatory (recommended), eSign,
   or both? Is an organisational document-signer certificate acceptable as an
   additional seal only?
2. **Authority:** who signs which PO values/categories; what board resolution or
   delegation wording is required; are two signatures needed above a threshold?
3. **Legal effect:** does the client require the PO signature to be legally
   binding on the company and accepted by vendors, or is it an internal control?
4. **Counterparties:** will vendor terms accept electronic POs? Do any vendors
   require wet signatures or a specific format?
5. **Retention and evidence:** retention period, B-LT vs B-LTA, who owns the BSA
   s. 63(4) certificate process, and where the evidence pack is stored.
6. **Remote signers:** is Aadhaar eSign acceptable for field/remote approvers?
7. **Data protection:** privacy notice, retention, access roles, and approval of
   India-based CA/TSA processors.
8. **Procurement:** budget owner, approved CA/TSA shortlist, token replacement
   policy, and target rollout date.

## 11. Recommendation

Proceed with a **pilot design** for individual Class 3 DSCs and **PAdES B-LT**,
using a local PKCS#11 agent and a CCA-licensed TSA, but do not start
implementation until decisions 1–3 and 5–6 above are confirmed. In parallel,
obtain quotes from at least three CCA-licensed CAs for a document-signing
certificate profile and FIPS 140-3 tokens, and have counsel confirm the
signer-authority and vendor-acceptance model. If remote/mobile signing is a
requirement, approve eSign as an explicit alternative rather than weakening the
token-custody model.

## 12. Sources (accessed 19 October 2026)

- IT Act 2000 ss. 3A, 5, 10A, 14, 15, 42 — India Code / eCourts India.
- Digital Signature (End Entity) Rules, 2015 (GSR 660(E), 25 Aug 2015) — full
  text, including Rule 7 (PAdES with CAdES, RFC 3161).
- CCA, *Interoperability Guidelines for Digital Signature Certificates*, v4.0,
  10 Feb 2026 — certificate profiles, key usage, EKU, CRL/OCSP.
- CCA, *Security Requirements for Crypto Devices*, v2.4, 14 Aug 2026 — FIPS
  140-2/3, 21 Sep 2026 migration, token requirements.
- CCA FAQ, <https://cca.gov.in/faq.html> — classes, organisational signer,
  long-term archival, timestamping, certificate expiry.
- CCA licensed CAs, <https://cca.gov.in/licensed_ca.html>.
- Bharatiya Sakshya Adhiniyam, 2023, s. 63(4) — electronic-record certificate.
- ETSI EN 319 142-1 v1.2.1 — PAdES baseline signatures B-B, B-T, B-LT, B-LTA.
