## [0.21.1](https://github.com/Fszta/parrant/compare/v0.21.0...v0.21.1) (2026-09-26)

### Bug Fixes

* **explorer:** use starlette's request-first TemplateResponse convention ([7661a20](https://github.com/Fszta/parrant/commit/7661a20f43d9d7e3fab65052761eab0358fc55c0))

## [0.21.0](https://github.com/Fszta/parrant/compare/v0.20.0...v0.21.0) (2026-09-02)

### Features

* **parser:** resolve lateral-flatten aliases to the flattened column ([59fc288](https://github.com/Fszta/parrant/commit/59fc288f8ec0ae9bf6d7fbd9389b0f9879e63b1b))
* **engine:** classify unparseable nodes as column-opaque, not parse failures ([679c67b](https://github.com/Fszta/parrant/commit/679c67b1e14d9a59574504fa7ec3b7b64741d4a6))

### Bug Fixes

* **semantic-diff:** classify comment/whitespace-only edits EQUIVALENT even when parse fails ([ea550a5](https://github.com/Fszta/parrant/commit/ea550a580e9ffad00365f392cda05d1570750a9d))

## [0.20.0](https://github.com/Fszta/parrant/compare/v0.19.0...v0.20.0) (2026-08-31)

### Features

* **impact:** declare unresolved column edges to prevent silent CI under-builds ([1f6cff1](https://github.com/Fszta/parrant/commit/1f6cff1e1176c23ce23120977e06999cd19a54fb))

### Bug Fixes

* **deps:** declare jinja2 as a runtime dependency ([ba9fc92](https://github.com/Fszta/parrant/commit/ba9fc92b30b17a71514cc3c9d36c51494cf0fe8b))

## [0.19.0](https://github.com/Fszta/parrant/compare/v0.18.1...v0.19.0) (2026-08-27)

### Features

* **metabase:** make --manifest optional when --adapter is set ([3b99e89](https://github.com/Fszta/parrant/commit/3b99e890245815a3b6ed69214fa50930c74c79cb))
* **explore:** static cross-boundary Metabase reach (no changeset needed) ([996b45d](https://github.com/Fszta/parrant/commit/996b45dd46c3b8befebc4813c286528099a71557))
* **metabase:** capture card/dashboard asset metadata ([ce4fc1d](https://github.com/Fszta/parrant/commit/ce4fc1d4eb21aad7a35c8fe0cd3a7b6e35d35772))
* **metabase:** add --timeout for the large /api/card fetch ([7f4d9ba](https://github.com/Fszta/parrant/commit/7f4d9bac0f635a5f8d26437e3403c51e732d84e8))
* **metabase:** support pMBQL (MBQL 5) card serialization ([983bb00](https://github.com/Fszta/parrant/commit/983bb00546429f1f0ddbe0e062bca62a3c84ea88))
* **metabase:** scope cards to connection and reuse unchanged dashboards ([cb189be](https://github.com/Fszta/parrant/commit/cb189bed9ede33912a7b69c0b0d4965864e0d51f))
* **metabase:** fetch dashboard details concurrently ([1172e9b](https://github.com/Fszta/parrant/commit/1172e9b07a6db576467ffda8db4c47af19acb8cf))
* **metabase:** add schema_version 2 with per-entity updated_at ([0c6ad5e](https://github.com/Fszta/parrant/commit/0c6ad5e89b56576aca701afd023f70df1b0e3297))
* **impact:** add per-model resolution status ([c8b44cb](https://github.com/Fszta/parrant/commit/c8b44cbe321993a32fef32a0898e79eb9e94f834))
* **impact:** emit policy-independent rebuild selection ([9d020cf](https://github.com/Fszta/parrant/commit/9d020cf5b5d361c7ab3c1866d519df79811d07a2))

### Bug Fixes

* **metabase:** stop /api/card pagination re-fetching the whole list forever ([da8b601](https://github.com/Fszta/parrant/commit/da8b6014e73194b5108e5190fd768a2a0a6989d6))
* **changeset:** fail safe on join/filter-only compiled-SQL change (#129) ([4b47ba0](https://github.com/Fszta/parrant/commit/4b47ba042d22d0e98cdc84f9ca586da3891c1648))
* **impact:** emit complete unanalyzable model lists in machine output ([7cc30ea](https://github.com/Fszta/parrant/commit/7cc30eaa66d75f6253000c2b251b5cbe318c089d))

## [0.18.1](https://github.com/Fszta/parrant/compare/v0.18.0...v0.18.1) (2026-08-25)

### Bug Fixes

* **lineage:** materialize star columns on catalog-missing targets (#123) ([260fb09](https://github.com/Fszta/parrant/commit/260fb09ab76d0db646176bdb3332a971513c1c4b))

## [0.18.0](https://github.com/Fszta/parrant/compare/v0.17.2...v0.18.0) (2026-08-25)

### Features

* **policy:** scaffold config-axis template and document config/inferred_meta axes ([0b81f56](https://github.com/Fszta/parrant/commit/0b81f5684dfdeb5402283d2fc4956362d5bd0a03))
* **policy:** add generic config predicate axis (config.grants.select etc.) ([47247d5](https://github.com/Fszta/parrant/commit/47247d57857adf0f009bd0b3479f5988941a7b19))
* **policy:** resolve meta by folding upstream lineage via effective.* predicates ([f9b1800](https://github.com/Fszta/parrant/commit/f9b1800608dc58a560d443c9ba9505a56ecd1876))

### Bug Fixes

* **policy:** treat null config set value as empty set ([311abcc](https://github.com/Fszta/parrant/commit/311abccd2cb129b9aad480b6043aaa94681fba0e))

## [0.17.2](https://github.com/Fszta/parrant/compare/v0.17.1...v0.17.2) (2026-08-24)

### Features

* **comment:** explain the verdict in the sticky PR comment ([7b85255](https://github.com/Fszta/parrant/commit/7b852556991044d228f50b44b13e32ebda230318))

### Bug Fixes

* **explorer:** restore SVG icons broken by stripped path moveto commands ([cbb0522](https://github.com/Fszta/parrant/commit/cbb05220be7d54753eace215b2f55ab1ff9856eb))
* **docs:** white header logo for contrast on the dark header ([b4d9f61](https://github.com/Fszta/parrant/commit/b4d9f61c549d82c98b4821d061cc3fef57c1ed27))
* **docs:** use the self-contained tile mark for the header logo ([917cac0](https://github.com/Fszta/parrant/commit/917cac044ddbe1184874f0764af596699a1673a7))

## [0.17.1](https://github.com/Fszta/parrant/compare/v0.17.0...v0.17.1) (2026-08-23)

### Features

* **explorer:** rebrand the explorer UI to Parrant ([48b82b0](https://github.com/Fszta/parrant/commit/48b82b0f56072587c40a1e714b6137c2bebc54dd))

## [0.17.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.16.1...v0.17.0) (2026-08-23)

### Features

* **cli:** add the policy init command ([fef0667](https://github.com/Fszta/dbt-column-lineage/commit/fef0667575d38447cb807b41e387ad9bdd0f4142))
* **policy:** manifest-introspecting policy scaffold generator ([c0b01df](https://github.com/Fszta/dbt-column-lineage/commit/c0b01dfa57b5222bc27553ec114948fadafa2912))
* **cli:** add the policy test command ([1f932ee](https://github.com/Fszta/dbt-column-lineage/commit/1f932eebdf9be64d7a534808b11a589202e1e32b))
* **models:** add policy-init scaffold report schema ([e163def](https://github.com/Fszta/dbt-column-lineage/commit/e163def83d999ad76a194f13caf9cdd5940950f6))
* **display:** render the policy-test backtest report ([92e32a2](https://github.com/Fszta/dbt-column-lineage/commit/92e32a29cb9690e9718c93bc2130c94e2e21892c))
* **backtest:** replay a policy over changesets with per-rule aggregation ([1c9f23e](https://github.com/Fszta/dbt-column-lineage/commit/1c9f23e1f18740595c46519ba85d9bbbfc20ad71))
* **changeset:** load changesets from json and add git-range helpers ([46cfd35](https://github.com/Fszta/dbt-column-lineage/commit/46cfd35a5837e799c7c38d3ee87ad8e521fb1ab2))
* **models:** add backtest result schema ([b706f2e](https://github.com/Fszta/dbt-column-lineage/commit/b706f2e87f0c829737ae656b57d64125f5995a3a))
* **display:** render applied and ineffective overrides in the report ([f9a633d](https://github.com/Fszta/dbt-column-lineage/commit/f9a633d07f4a2ba5152529921e5666e5d9edc271))
* **policy:** cap policy rule hits with matching overrides ([86ed5cd](https://github.com/Fszta/dbt-column-lineage/commit/86ed5cd381cc4737815f47c4cee75e4994933559))
* **verdict:** honor overrides in the gate ruling ([a1043b6](https://github.com/Fszta/dbt-column-lineage/commit/a1043b65cf4c8f76ba2717c1299d0cb350c73daf))
* **changeset:** attach override directives to column changes ([2a9c0f3](https://github.com/Fszta/dbt-column-lineage/commit/2a9c0f3a51febdcb8c80102270dbfc0dd8b82eaf))
* **parser:** parse -- lineage:allow-change/allow-break pragmas ([4d1e296](https://github.com/Fszta/dbt-column-lineage/commit/4d1e296fc9210dce41520c191b020cb9ed8980d2))
* **models:** add override directive and rule-hit override fields ([a7ddaa0](https://github.com/Fszta/dbt-column-lineage/commit/a7ddaa0c598a5c11a73ccfd9c736f5400418fe6e))
* **policy:** add built-in semantic-severity gate knobs ([07ae7fb](https://github.com/Fszta/dbt-column-lineage/commit/07ae7fbc1cb977acb5d92ca51852af74169630a4))
* **verdict:** let the semantic axis lift a safe ruling to review ([7035986](https://github.com/Fszta/dbt-column-lineage/commit/70359869826f4f14091f84e981fce5c9d398d2c9))
* **impact:** add --explain to surface why each column was flagged ([400e2f6](https://github.com/Fszta/dbt-column-lineage/commit/400e2f69c66e214588763cdf5bad8dd5f6d38fcc))

## [0.16.1](https://github.com/Fszta/dbt-column-lineage/compare/v0.16.0...v0.16.1) (2026-08-23)

### Bug Fixes

* **semantic-diff:** stop reporting NULL-unsound boolean rewrites as equivalent ([b0dd07d](https://github.com/Fszta/dbt-column-lineage/commit/b0dd07db3e35862113b79ee658d96a47d05f9563))

## [0.16.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.15.0...v0.16.0) (2026-08-23)

### Features

* interactive lineage & impact explorer + CLI wiring ([3b16305](https://github.com/Fszta/dbt-column-lineage/commit/3b16305aeef09273590655e1746dfe0cf8a6ff4d))
* cross-boundary impact into BI dashboards (Metabase connector) ([b37f0d4](https://github.com/Fszta/dbt-column-lineage/commit/b37f0d4c6c3aec77b469867e78a08527ef386e73))
* metadata-agnostic policy gate ([581eaf5](https://github.com/Fszta/dbt-column-lineage/commit/581eaf5412b0f2dd20d3f24386bb9896a5b4689b))
* semantic change categorization on a swappable lineage provider ([301d8c2](https://github.com/Fszta/dbt-column-lineage/commit/301d8c2a76f66c79a1b4193795b5b289ae077759))

## [0.15.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.14.0...v0.15.0) (2026-08-21)

### Features

* **explorer:** render dbt descriptions as Markdown in the model card ([faeefaf](https://github.com/Fszta/dbt-column-lineage/commit/faeefaf95d36690d32b7676eb4afb58d5f212dbb))
* **explorer:** add "Show tests" toggle for dbt tests in graph + impact panel ([a0eb15f](https://github.com/Fszta/dbt-column-lineage/commit/a0eb15f3256930fa64813c5f5fd6c33e26e9922a))
* **explorer:** cap fan-out with "+N more" progressive disclosure ([e348f07](https://github.com/Fszta/dbt-column-lineage/commit/e348f072d4153dc9119f4b8cd00d25f78e748470))
* **impact:** test-aware SAFE/REVIEW/BLOCK verdict, CI gate, owner routing ([4165d1b](https://github.com/Fszta/dbt-column-lineage/commit/4165d1b5e3c722f07a595f9e0fe0c34826c17ef8))
* **artifacts:** ingest dbt test nodes and expose column/model/reference test indexes ([dca799b](https://github.com/Fszta/dbt-column-lineage/commit/dca799b02831b9b73ee528bdc470e90918d4fe3f))
* **explorer:** render row-set nodes with predicate hover + click card (frontend) ([ac2a78e](https://github.com/Fszta/dbt-column-lineage/commit/ac2a78ee0717a9c8843f95dc00ec042e5099e3b7))
* **explorer:** expose row-set dependents as distinct graph nodes (backend) ([acc77ec](https://github.com/Fszta/dbt-column-lineage/commit/acc77ec99f174025c710a1a005af668bff35490b))
* **impact:** surface model and column descriptions in the JSON output and explorer ([b64e74a](https://github.com/Fszta/dbt-column-lineage/commit/b64e74a66818e5d0ceaa40c3897e924e45c6f7a1))

### Bug Fixes

* **impact:** propagate row-set dependents along the value chain ([085c967](https://github.com/Fszta/dbt-column-lineage/commit/085c967408d3763a7d73d4e478338f83ae9e6a65))
* **artifacts:** recover compiled SQL by filename when target/compiled drifts ([d1067be](https://github.com/Fszta/dbt-column-lineage/commit/d1067be9c4d2288c38d8fd306fff1a7569dc24e9))

## [0.14.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.13.0...v0.14.0) (2026-08-20)

### ⚠ BREAKING CHANGES

* drops support for Python 3.9; the minimum supported version is now Python 3.10.

### Features

* **ci:** note when structural checks are skipped without a catalog ([23b88fc](https://github.com/Fszta/dbt-column-lineage/commit/23b88fc083083a3e772a5d0f08586bb2ea9457a8))
* **cli:** add --version flag ([bce9d67](https://github.com/Fszta/dbt-column-lineage/commit/bce9d67b90998a0e6279cf328df3449e35b1bce2))
* **action:** add PR-comment credit, outputs, and pip cache ([d6e1031](https://github.com/Fszta/dbt-column-lineage/commit/d6e103166de33ec750b2f826d526db38a41ab082))
* **impact:** restructure the CI impact PR comment for reviewer-first triage ([286e403](https://github.com/Fszta/dbt-column-lineage/commit/286e403dbc930c9c1ab03f2c9e9232776ed78565))

### Bug Fixes

* **parser:** expand adapter-to-dialect mapping and warn on unknown dialect ([a195fc7](https://github.com/Fszta/dbt-column-lineage/commit/a195fc7a6290af28c618f2d65c6d83cf29fcabbc))
* **parser:** trace all UNION branches in column lineage ([2844bba](https://github.com/Fszta/dbt-column-lineage/commit/2844bbaf5a0f7092bd9b24a2825b2e108059497d))

## [0.13.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.12.0...v0.13.0) (2026-08-19)


### Features

* **docs:** realistic two-chain lineage graph + emphasize column/impact ([986c7b8](https://github.com/Fszta/dbt-column-lineage/commit/986c7b879112adf08f49fc6e6b11204f962f3069))
* **docs:** redesign documentation site to match explorer design system ([c6b17ab](https://github.com/Fszta/dbt-column-lineage/commit/c6b17abae450dfe08e3c1b46ada3a775e82a0e8d)), closes [#5E6AD2](https://github.com/Fszta/dbt-column-lineage/issues/5E6AD2)
* **explorer:** align app frontend with the docs design system ([b8cf0b1](https://github.com/Fszta/dbt-column-lineage/commit/b8cf0b1e1bcbaa14a9f5aeaba923aad38eaffa68))


### Bug Fixes

* **explorer:** dimensional database icon in the tree ([101d794](https://github.com/Fszta/dbt-column-lineage/commit/101d79439e662d3bedc53ece85d25c64a3d029e2))

## [0.12.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.11.0...v0.12.0) (2026-08-18)


### Features

* **changeset:** per-column logic-change precision ([1d0a843](https://github.com/Fszta/dbt-column-lineage/commit/1d0a843749351eb9dc4895ecf6c8b1dbdd2cbcb2))
* **comment:** criticality-structured PR comment ([d686012](https://github.com/Fszta/dbt-column-lineage/commit/d686012d3f0269203616388f34709f54d7c411da))
* **explorer:** generic 'recomputed / output changes' wording ([6c046bd](https://github.com/Fszta/dbt-column-lineage/commit/6c046bdfa83154da7dfa5c912ef08228bbfb38d3))
* **impact:** surface predicate/row-set dependents as a distinct severity ([4b118e3](https://github.com/Fszta/dbt-column-lineage/commit/4b118e3263d0950c1a99c06441f68e3426778409))
* **parser:** resolve predicate (filter/join) column dependencies ([168ccda](https://github.com/Fszta/dbt-column-lineage/commit/168ccdaa5c6113a7d0696f5e7f2841cd32268369))


### Bug Fixes

* seed model universe from manifest so uncatalogued models are analyzable ([#69](https://github.com/Fszta/dbt-column-lineage/issues/69)) ([78e372b](https://github.com/Fszta/dbt-column-lineage/commit/78e372b7786baa579a0f29d0bb88cdc9abf8a331))

## [0.11.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.10.0...v0.11.0) (2026-08-18)


### Features

* **explorer:** add design token system and dark-mode foundation ([9018328](https://github.com/Fszta/dbt-column-lineage/commit/9018328480b1e80f71b1a3a7e821c1af8123ec33))
* **explorer:** modernize graph canvas and node cards ([b6a5e70](https://github.com/Fszta/dbt-column-lineage/commit/b6a5e7047592cd1eaf6744bf706273e5b2408606))
* **explorer:** redesign impact panel and relationship summary ([b59959a](https://github.com/Fszta/dbt-column-lineage/commit/b59959a00050d73608365eecc5b1b7bcc36306ef))
* **explorer:** restyle sidebar, tree, controls and primary button ([95f4121](https://github.com/Fszta/dbt-column-lineage/commit/95f4121c7536e6f8d2d0c5a38b7091407ff8317e))
* **explorer:** surface impact confidence & coverage in the UI ([086c30c](https://github.com/Fszta/dbt-column-lineage/commit/086c30c7cc22cfac2fbf06134334ebd0afc2caab))
* **explorer:** thermal-rail highlight and edge hierarchy on select ([aeb03e6](https://github.com/Fszta/dbt-column-lineage/commit/aeb03e6c395565365555bc7c01d36cc4c79fd9a9))
* **explorer:** wire theme toggle, brand mark and empty state markup ([3265f49](https://github.com/Fszta/dbt-column-lineage/commit/3265f491e3114ec1109ce6c430e7a7cce85502b0))
* **impact:** add coverage & confidence signal ([1451806](https://github.com/Fszta/dbt-column-lineage/commit/14518063eb2ebb9d4f20a760b95e44da997fb392))
* **ui:** explorer readability & interaction polish ([f7cee98](https://github.com/Fszta/dbt-column-lineage/commit/f7cee9808f8ad286a270fde3c4ac66fad980b578))


### Bug Fixes

* declare requests as a runtime dependency ([#68](https://github.com/Fszta/dbt-column-lineage/issues/68)) ([a22488f](https://github.com/Fszta/dbt-column-lineage/commit/a22488f10b3e737dfedb7054c45d68591e105f4e))

## [0.10.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.9.0...v0.10.0) (2026-08-14)


### Features

* **impact:** --scope-git to restrict two-manifest diff to branch-changed models ([f089445](https://github.com/Fszta/dbt-column-lineage/commit/f0894455098b47c6fdf4a6ee4ec9a3a0d5988209))


### Bug Fixes

* **ci:** pin Poetry to 2.1.3 in publish workflow ([5f4181e](https://github.com/Fszta/dbt-column-lineage/commit/5f4181e533bf2ce5706e9b04b22af2a7383f07af))

## [0.9.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.8.0...v0.9.0) (2026-08-14)


### Features

* **cli:** add machine-readable --format json for AI-first consumption ([9211c1f](https://github.com/Fszta/dbt-column-lineage/commit/9211c1facbbf49bf1e3fba06c05dc3b53f1e01b8))
* **impact:** add diff-driven impact command ([b7eb165](https://github.com/Fszta/dbt-column-lineage/commit/b7eb165ca824527e4c4822efbb454bdb4c776314))
* **impact:** CI mode with sticky PR comment and severity gate ([dd787af](https://github.com/Fszta/dbt-column-lineage/commit/dd787af998388fba0d7743147a86c3bb601a64c8))
* **ui:** set initial state when no model is selected ([fc92a03](https://github.com/Fszta/dbt-column-lineage/commit/fc92a03088b2b765d7199824266d132ce19f5c82))


### Bug Fixes

* **catalog:** read schema/database/name from node metadata ([c2c9053](https://github.com/Fszta/dbt-column-lineage/commit/c2c9053476119f17e0816ee6c1a381a18b4f68ea))
* **display:** handle exposures/sources sets in downstream output ([c2b8712](https://github.com/Fszta/dbt-column-lineage/commit/c2b8712c7d73cde856ca5dd909b8c6ebef8bb9da))
* **manifest:** fall back to on-disk compiled SQL when manifest lacks compiled_code ([1a1af9a](https://github.com/Fszta/dbt-column-lineage/commit/1a1af9a3fed38bcfc28301e41de3c611860d4be8))
* **parser:** resolve transitive CTE aliases to the base model ([0be42ea](https://github.com/Fszta/dbt-column-lineage/commit/0be42ea158a782c4c1e64bbe71df6104143a59b1))

## [0.8.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.7.0...v0.8.0) (2025-11-30)


### Features

* add model details card component ([cf5aa7b](https://github.com/Fszta/dbt-column-lineage/commit/cf5aa7b49dfa62719fbf1e0742eca2d9a99b923c))
* add model details endpoint with desc and tags ([4cdac70](https://github.com/Fszta/dbt-column-lineage/commit/4cdac70d30b0cd762f5239d132ce9818f3bb11e8))
* **lineage:** add expand upstream lineage capability ([ef89f35](https://github.com/Fszta/dbt-column-lineage/commit/ef89f352567379dbf93aa0f2d01a083881dfbaca))
* make col relationship summary card collapsible ([21a6135](https://github.com/Fszta/dbt-column-lineage/commit/21a613574f7fb299e5e7cd91bf280d28e442c146))
* **ui:** add glassmorphism effect and polish interactions ([017b253](https://github.com/Fszta/dbt-column-lineage/commit/017b253f105b09d98fda183908161bdad0f1aa1e))
* **ui:** enhance explorer tree with badges, node count and search highlight ([f73d88b](https://github.com/Fszta/dbt-column-lineage/commit/f73d88b3303c2970ffe9a4a6ac684ba486dd3ce7))
* **ui:** improve graph viz with better shaddow and hover states ([636df01](https://github.com/Fszta/dbt-column-lineage/commit/636df0135c7e2e003711e7ff7cf084a4e1f7c67b))


### Bug Fixes

* **lineage:** align exposure edges horizontally with model edges ([38fed02](https://github.com/Fszta/dbt-column-lineage/commit/38fed021bb899497703978ff7361ab0166685a00))
* return full upstream lineage chain in api, not only n-1 ([0ece04a](https://github.com/Fszta/dbt-column-lineage/commit/0ece04a88902ee3c9eb41a04ac1f9767cf4eb2e4))

## [0.7.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.6.0...v0.7.0) (2025-11-28)


### Features

* **folder-tree:** add tooltip for truncated models ([8043352](https://github.com/Fszta/dbt-column-lineage/commit/80433521975d9f509e28abb09ed3947ed76dcbe1))
* **impact-analysis:** group transformations by model with toggle ([60e5044](https://github.com/Fszta/dbt-column-lineage/commit/60e5044bd55bb70fa8fcee042b13c812335fad9f))
* **lineage:** add tooltips for truncated column and model names ([fdf141e](https://github.com/Fszta/dbt-column-lineage/commit/fdf141e65facc845e2dbc70216b16fd808182281))
* **snapshots:** add dbt snapshots support ([a84bf63](https://github.com/Fszta/dbt-column-lineage/commit/a84bf6314e07b29547e1c830aa17185f4492d16d))


### Bug Fixes

* **impact-analysis:** close buttons for impact/relationship summary cards ([6b9ddf1](https://github.com/Fszta/dbt-column-lineage/commit/6b9ddf127677037d8aecb949b700131027236dce))
* **impact-analysis:** truncate long names and fix badge pos ([f9e77b2](https://github.com/Fszta/dbt-column-lineage/commit/f9e77b283fa14294f9e6d913bcc57121216a2fab))

## [0.6.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.5.0...v0.6.0) (2025-11-26)


### Features

* add column relationship summary card to lineage view ([e3b1ce9](https://github.com/Fszta/dbt-column-lineage/commit/e3b1ce97cb3046d3176b5a29a40102621f3fc286))
* add expand/collapse functionality for lineage graph ([#25](https://github.com/Fszta/dbt-column-lineage/issues/25)) ([f9a88a6](https://github.com/Fszta/dbt-column-lineage/commit/f9a88a6f0f3e299f11c498b2aad56a533408d4fd))
* add search input and filtering to column selector ([ab6c8c2](https://github.com/Fszta/dbt-column-lineage/commit/ab6c8c294612132f8f4ad9528c0efa710fabd170))
* add strip_sql_comments utility function ([28cdf95](https://github.com/Fszta/dbt-column-lineage/commit/28cdf95dffa5090d63dcafcd7bf6f144232dbec6))
* improve SQL parser with forward references, EXCLUDE clause, and nested subquery support ([#24](https://github.com/Fszta/dbt-column-lineage/issues/24)) ([4a2bc73](https://github.com/Fszta/dbt-column-lineage/commit/4a2bc739c64596bf201d7a94b3b0cd4e94013a1e))


### Bug Fixes

* handle uppercase source ([85abcee](https://github.com/Fszta/dbt-column-lineage/commit/85abcee2f9ee9afb5469a057cb760aad596aa033))
* non deterministic behavior ([7b27132](https://github.com/Fszta/dbt-column-lineage/commit/7b271326f40bbb758a087a75cde9ca8201453e26))
* strip SQL comments in parser ([db91629](https://github.com/Fszta/dbt-column-lineage/commit/db9162911f943e57776511fc58d8a5cb340d9025))
* strip SQL comments in service layer ([4f617c4](https://github.com/Fszta/dbt-column-lineage/commit/4f617c40e35c06ef7f4791390b26e0d799a1f863))

## [0.5.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.4.0...v0.5.0) (2025-11-16)


### Features

* add exposures to lineage ([#18](https://github.com/Fszta/dbt-column-lineage/issues/18)) ([a03f26c](https://github.com/Fszta/dbt-column-lineage/commit/a03f26ccb5e3f41f60d303413ba09f699c9bbe49))
* add impact analysis to understand downstream effects of col changes ([#21](https://github.com/Fszta/dbt-column-lineage/issues/21)) ([1352fb4](https://github.com/Fszta/dbt-column-lineage/commit/1352fb4e7317d5322a779a9955bfd1b6100de11e))
* enhance explore panel ui ([#19](https://github.com/Fszta/dbt-column-lineage/issues/19)) ([0a0e5d6](https://github.com/Fszta/dbt-column-lineage/commit/0a0e5d6a82a8f90e0688021494429565272131c1))


### Bug Fixes

* position nodes based on data flow, not selected node ([#15](https://github.com/Fszta/dbt-column-lineage/issues/15)) ([66805ec](https://github.com/Fszta/dbt-column-lineage/commit/66805ecdacc2d7280d70341e3be7e127129ff1e5))

## [0.4.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.3.1...v0.4.0) (2025-11-13)


### Features

* add explorable dbt project folder tree ([#11](https://github.com/Fszta/dbt-column-lineage/issues/11)) ([689902c](https://github.com/Fszta/dbt-column-lineage/commit/689902cf46177ce808390be61a81f88aaf5d8dab))
* add mssqlserver / tsql adapter support ([#13](https://github.com/Fszta/dbt-column-lineage/issues/13)) ([30632fd](https://github.com/Fszta/dbt-column-lineage/commit/30632fd9899f421b98cfd955aab8f41208627ef1))

## [0.3.1](https://github.com/Fszta/dbt-column-lineage/compare/v0.3.0...v0.3.1) (2025-04-12)


### Bug Fixes

* **lineage:** overlapping model boxes ([#6](https://github.com/Fszta/dbt-column-lineage/issues/6)) ([b582bea](https://github.com/Fszta/dbt-column-lineage/commit/b582bea14077e0122e6d9e090cf1d16d0711b202))

## [0.3.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.2.1...v0.3.0) (2025-04-09)


### Features

* add dialect support for SQL parser ([#5](https://github.com/Fszta/dbt-column-lineage/issues/5)) ([a1060e7](https://github.com/Fszta/dbt-column-lineage/commit/a1060e7ef9f5142ed2ea2912e7075c6da4a3887c))

## [0.2.1](https://github.com/Fszta/dbt-column-lineage/compare/v0.2.0...v0.2.1) (2025-04-08)


### Bug Fixes

* model box drag behavior ([#3](https://github.com/Fszta/dbt-column-lineage/issues/3)) ([bcab221](https://github.com/Fszta/dbt-column-lineage/commit/bcab221dabcda80738aa13f5f9a5145ae4f4bc13))

## [0.2.0](https://github.com/Fszta/dbt-column-lineage/compare/v0.1.1...v0.2.0) (2025-04-08)


### Features

* add explore CLI command for interactive lineage visualization ([#1](https://github.com/Fszta/dbt-column-lineage/issues/1)) ([f804bbc](https://github.com/Fszta/dbt-column-lineage/commit/f804bbc19ad2dfdc90d63cdaed88802646745d00))

## [0.1.1](https://github.com/Fszta/dbt-column-lineage/compare/20ee116563dd2eff3233abb279531105168e5c2a...v0.1.1) (2025-03-30)


### Features

* add cli with display format ([7243593](https://github.com/Fszta/dbt-column-lineage/commit/72435938c430343ee22987437db71e9b063f5a78))
* add html lineage ([dec8602](https://github.com/Fszta/dbt-column-lineage/commit/dec86028fd90eef934046c1b0bf54ae2ddd2a92f))
* handle select star ([023a4e4](https://github.com/Fszta/dbt-column-lineage/commit/023a4e46d554077934943146069a62f7fd49bd71))
* handle star refs ([a5795de](https://github.com/Fszta/dbt-column-lineage/commit/a5795de5f91d2ed2896ca3e8ccfbfaaa9b18efd9))
* improve html display ([006a560](https://github.com/Fszta/dbt-column-lineage/commit/006a560b354a632597bbfb0668f91e00a2d689db))
* support html render ([d6a067d](https://github.com/Fszta/dbt-column-lineage/commit/d6a067db7c6cb877c32203e64d291bdd682c3f22))


### Bug Fixes

* raw table name ([20ee116](https://github.com/Fszta/dbt-column-lineage/commit/20ee116563dd2eff3233abb279531105168e5c2a))
* test csv file name ([60b8853](https://github.com/Fszta/dbt-column-lineage/commit/60b885307ae823c716287194184ef4e5f33c4ef7))
* use source name instead of identifier ([2e83693](https://github.com/Fszta/dbt-column-lineage/commit/2e8369398db0b4ec40b48eb34d19e1f7d6e5dc43))

