# Local database and document-storage restore drill

On 2026-10-25, the isolated `atcraft-release-20261024` rehearsal was backed up
and restored into a separate database and Docker storage volume. This verifies
the captured local candidate at migration `20261031_0100`; it is not evidence of
a production deployment or off-box recovery.

The capture stopped only the rehearsal's API, batch worker, notification worker
and scheduler. They resumed healthy after the database and storage snapshots
were captured. PostgreSQL, Redis, the existing `atcraft_erp` database and other
projects were left running. The capture and service-resumption interval was
82.25 seconds.

The database archive was restored with `pg_restore --single-transaction
--exit-on-error --no-owner --no-privileges` into
`atcraft_restore_20261025_5e1943db`, created from `template0`. Storage was restored
into `atcraft-release-20261024-restore-storage-5e1943db`, with archive paths and
entry types checked before extraction. Existing restore targets were preserved;
verification refuses an unexpected populated target rather than clearing it.

| Verification                                        | Result                                                                             |
| --------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Migration revision                                  | `20261031_0100`, identical to the capture                                          |
| Public table row counts                             | All 137 tables match; 2,452 rows in total                                          |
| Immutability trigger definitions and enabled states | All five match                                                                     |
| Attempted immutable mutations                       | Nine operations rejected with SQLSTATE `42501`                                     |
| Document metadata                                   | All 10 storage keys, SHA-256 values and sizes match restored bytes                 |
| Storage content                                     | All 10 files match their captured hashes and sizes; 14,207 bytes                   |
| Storage ownership and permissions                   | All 28 captured directory/file entries match                                       |
| Application storage access                          | Unprivileged UID/GID 10001 can read all 10 files and read/write all 18 directories |
| Application health                                  | API and all three worker/scheduler services healthy before and after verification  |

The mutation checks attempted UPDATE and DELETE on audit logs, inventory
transactions, inventory cost consumptions and serial conversion history, plus
UPDATE on login history. A temporary login-history row made that last check
meaningful despite the captured history being empty. The entire proof
transaction was rolled back, then every snapshot comparison was repeated to
verify that the checks changed no restored data.

Safe archive extraction discards Unix ownership by default. The drill restored
the trusted archive's captured ownership and permissions after validating its
paths and checksum, then checked access as the application's unprivileged user.
Matching document bytes alone would not prove that later uploads could use the
restored directory tree.

Archive SHA-256 values:

- Database: `0109049ad26d880039fc1115ff1c22e340c91d9182f2001fec16e3ac14f0030e`
- Storage: `e93f4884893019437b45a7e82bc43a5c63bf74574374b6550c18af17f4e6f1e6`

The sanitized machine-readable result is retained outside Git at
`%TEMP%\atcraft-release-20261024\restore-evidence.json`; archives, document
identifiers, credentials and the private capture manifest remain outside the
repository. The restored database and volume were retained for inspection.

Production still requires the deployment runbook's encrypted off-box backups,
retention policy, production-sized restore rehearsal, host recovery checks and
operational ownership. This small local dataset does not establish production
recovery time or recovery point objectives. See
[`deployment.md`](../deployment.md#backups) for those release requirements.
