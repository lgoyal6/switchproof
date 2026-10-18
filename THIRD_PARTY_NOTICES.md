# Third-party components

Vendored sources retain their original licenses and are unchanged.

| Component | Revision | License | Use |
| --- | --- | --- | --- |
| [FalkorDB/Neo4j-to-FalkorDB](https://github.com/FalkorDB/Neo4j-to-FalkorDB) | `6ca9309cbf59690b3588db54b08ad48cad938880` | MIT | Actual CSV export/import in a temporary subprocess |
| [neo4j-examples/movies-python-bolt](https://github.com/neo4j-examples/movies-python-bolt) | `f86445ed1e9b5b12fb1b2faade5e209d6feec9b1` | Apache-2.0 | Actual Flask handlers and serializers, explicit driver adapters |
| [seperman/deepdiff](https://github.com/seperman/deepdiff) | Python package version in `uv.lock` | MIT | Typed result differences |
| [FalkorDB/benchmark](https://github.com/FalkorDB/benchmark) | `e1cc4559d5a65c95921297f28e9cb320c18a61f0` | MIT | Investigated benchmark reference; not executed in reported measurements |

FalkorDB, Neo4j Python driver, FastAPI, Flask, Anthropic SDK, React, Vite, Lucide, and other dependencies retain their package licenses. Exact versions and integrity hashes are in `uv.lock` and `web/package-lock.json`. FalkorDB and Neo4j container tags are pinned in `compose.yaml`; these are local runtime services, not vendored engine implementations.

Source license texts are in `vendor/migration/LICENSE` and `vendor/movies/LICENSE`. SwitchProof does not claim authorship of the migration implementation, Movies application, graph-copy primitive, or result-diff algorithm.

AI development assistance was used for implementation, investigation, and verification. Runtime planner provenance is recorded per run. Test decisions are explicitly labeled and do not represent live inference.
