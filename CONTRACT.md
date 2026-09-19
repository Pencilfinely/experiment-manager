# Experiment Manager implementation contract (0.2 preview)

Python >=3.10; core/agent/SDK are standard library only. The optional external SASRec adapter additionally imports torch/numpy. Package: `expman`. Algorithm source and datasets remain separate from the manager.

## Task specification (immutable after submission)

```json
{"name":"demo","algorithm":"demo","group":"example","metric_protocol":"demo-v1",
 "backend":"demo","params":{"steps":8,"delay":0.2,"seed":42},
 "assets":[],"tags":[],"priority":0,
 "resources":{"gpu_memory_mb":0,"cpu":1,"ram_mb":256,"exclusive":false},
 "source":null,"command":[],"environments":[]}
```

Docker tasks instead use backend=docker, GPU memory >0, source={repo: node-readable Git remote URL or node-local absolute Git repository path, commit: full 40-char SHA}, command=list of argv, environments=[{profile:name,image:repo@sha256:64hex}]. All command execution uses argv without shell. Assets are versioned string IDs mapped by node config. `command` runs in /workspace/code. Params in /workspace/run/params.json; EXPERIMENT_OUTPUT=/workspace/run; EXPERIMENT_PARAMS points to params file; EXPERIMENT_ASSETS is JSON mapping IDs to /assets/ID. EXPERIMENT_RESUME is 0/1. Code/assets mounted read-only, only run output writable. New containers use the Linux agent's current UID:GID via --user, matching bind-mounted input/output ownership; images must support running under that identity. The execution record includes container_user. A named run ID must be a UUID hex string. Initial support: one GPU per task, many tasks per GPU/node.

## Shared functions

- common.now(): float timestamp; common.atomic_json(path,obj); common.read_json(path,default=None); common.sha256_file(path); common.safe_child(root,relative); common.validate_task(spec) -> normalized task or ValueError; common.expand_grid(spec,grid)->list task specs (max256); common.api_request(url,token,payload=None,timeout=10) -> JSON (GET if None, POST otherwise).
- scheduler.eligible(task,snapshot)->bool (node tags/assets/profiles/allowed GPUs).
- scheduler.select_device(task,snapshot,active)->dict {gpu_uuid,environment} or None. active=[{spec:task,gpu_uuid:uuid}] for preparing/starting/running jobs. Conservative unknown GPU allocation headroom. Snapshot policy run_enabled/max_running/cpu_budget/ram_budget_mb; gpus each {uuid,name,total_mb,free_mb,reserve_mb,max_jobs,profiles:[profile names]}. snapshot profiles maps profile name -> pinned image; assets list IDs; tags list; backend demo allowed only via allow_demo=true. CPU/RAM/disk telemetry gates starting work.
- scheduler.choose_node(task,nodes,counts)-> node ID or None. nodes list {id,last_seen,snapshot}; counts map node ID -> outstanding assigned jobs. Ignore heartbeat age>45s; use policy.max_prefetch (default4) and policy.speed (default1). This is heuristic scheduling, not global optimum.

## Hub API

Hub class Hub(root), `serve(root, host='127.0.0.1', port=8765)`. Config root/hub.json created by Hub: admin_token plus nodes tokens. `add_node(node_id)` creates token. SQLite persistent jobs & nodes & command flags; threading lock/transactions. Nodes must be explicitly added by an administrator. Tokens never returned to unauthenticated clients. The release controller launcher listens on all local interfaces and persists its selected port.

- GET / serves static/index.html and /app.js, /timing.js, /style.css public. All /api require bearer auth. Browser uses sessionStorage for token. No CORS.
- GET /api/state (admin): {jobs:[{id,spec,node_id,state,detail,updated,attempt}],nodes:[{id,last_seen,snapshot,mode}],...}. node tokens do not access admin API.
- GET /api/setup-info (admin): suggested local IPv4 controller addresses; these are not connectivity guarantees.
- POST /api/enroll (admin): {node_id,hub_url}; validates the request before adding the node and returns a schema-1 pairing file containing only node_id, hub_url and the node token. Re-export preserves the existing identity.
- GET /api/node-info (node): returns node_id and paired=true for an authenticated worker.
- POST /api/jobs (admin): {spec,grid?:object,request_id:string}; return {ids:[]}. Persist request_id for duplicate POST retries.
- POST /api/node-mode (admin): {node_id,mode:'run'|'drain'}; drain prevents new assignments and starts, does not kill running jobs.
- POST /api/action (admin): {job_id,action:'stop'|'resume'|'cancel'}; resume only terminal interrupted/paused/failed jobs; never reassign offline jobs. Node acknowledges commands by monotonically increasing command_id; preserve pending until ack.
- GET /api/job?id=ID (admin): job + events (recent) + artifacts (name,size,sha256) + metrics (from reports).
- GET /api/results.csv (admin): downloadable params + latest metrics, flatten columns.
- POST /api/sync (node): {node_id,snapshot,reports:[{id,seq,state,detail,attempt,metrics,command_ack}]}. Validate each reported job belongs to node. Ignore duplicate/old seq, terminal cannot regress unless a resume command permits. Response {jobs:[{id,spec,command_id,action}],mode,ack:{id:seq}}. Deliver all outstanding owned jobs repeatedly; node upserts without resetting local state. Assign queued jobs with choose_node; ownership never expires automatically. Offline node shown stale, not failed/requeued.
- POST /api/upload (node): {job_id,name,sha256,size,offset,data:base64}. max chunk512KiB decoded. Ownership check; safe relative paths; store partials keyed job+sha; server returns {offset,complete}; offset=0,data='' queries current offset; sequential append then verify SHA256 & atomic rename to content-addressed archive. Full snapshot of a file is immutable; resume bytes after disconnect. Invalid hash never completes. Artifact metadata records name+sha (preserve versions). Exclude symlinks, reject path traversal. Root/<archives>/<job>/<sha> stores content; download GET /api/artifact?job_id=...&sha256=... admin only.

## Agent

`Agent(config_path)`, `tick()` one cycle; `run(config_path, once=False)` loop. Config JSON: {node_id,hub_url,token,root,allow_demo:false,poll_seconds:5,allowed_repos:[],assets:{id:absolute path},profiles:{name:{image:pinned digest,gpu_name_patterns:[substring],verified:true}},gpu_policy:{UUID:{reserve_mb:2048,max_jobs:1}},policy:{run_enabled:true,max_running:2,max_prefetch:4,cpu_budget:4,ram_budget_mb:8192,speed:1},tags:[]}.

Persistent local SQLite tasks and seq/attempt; root/runs/ID output; root/repos/ hashed-repo Git cache; root/worktrees/ID immutable checkout. Single-agent file lock, survives process death without stale permanent lock. Never expose Docker socket to containers. Agent runs on WSL/Ubuntu for docker backend, Windows supported only for demo backend. Prepares source/image while online; no new image pull during offline ready->run. All required assets must exist. Verify repo allowlist, full commit, image ID cached. Save ready only after preparation succeeds. Detect Docker availability and nvidia-smi safely; missing telemetry => no GPU start. Do not call install/update or restart services.

Deterministic container name expman-ID-attempt, docker create then start, persist intent before external execution; recovery inspect container before rerun. No --rm; inspect ended containers to recover exit status, never assume missing running container completed. Demo executes only bundled example via Python (no arbitrary host command). On restart running demo => interrupted, no duplicate process launch. Use OS file lock to prevent two agent loops. GPU selection immediately before create; account active reservations atomically in agent loop. Retain containers by default for recovery; explicit cleanup deferred.

Each new Docker container receives both `--gpus device=<assigned UUID>` and an explicit `CUDA_VISIBLE_DEVICES=<same UUID>` environment value. The latter is needed on the observed WSL node where Docker's request alone still exposes two GPUs to CUDA. Use the assignment for the current attempt, never the agent host's CUDA mask or a hardcoded index. Persist `cuda_visible_devices` in execution-attempt metadata; existing containers retain their original environment during reconciliation. This selects the GPU for CUDA applications and maps it to logical device 0; it does not provide a device isolation boundary against container code changing its own environment.

Stop: write root/runs/ID/STOP, request cooperative checkpoint; after configured grace timeout stop only own container, mark interrupted if no valid checkpoint. SDK supports should_stop(), metric(step,**values), publish_checkpoint(file,step), finish(result). Checkpoint manifest has path/hash/step; validates complete file before `paused`. Resume increments attempt, same output directory, sets EXPERIMENT_RESUME=1, removes STOP; checkpoint passed through persistent output. No automatic restart of failed scientific jobs.

Metrics: sdk appends metrics.jsonl and atomic latest_metrics.json; node reports latest metrics while running, stdout attempt-N.log. Archived files use closed-file snapshot: config and final files after terminal status, not live SQLite, not half-written checkpoints. Sync events first; chunk transfers bounded per tick, reconnect retries. Agent continues polling/running if Hub unavailable. Already cached ready tasks run offline; global new assignments paused.

## Experiment timing

Experiment timing is an optional worker report field: `timing={started_at,finished_at,elapsed_seconds,observed_at,complete}`. Timestamps are Unix seconds; start/finish may be null. Duration accumulates running intervals across attempts, excluding preparation, queueing and stopped periods. Workers persist timing locally and report it independently of metrics. Docker lifecycle timestamps recover execution that finishes while the agent is offline; unknown stop times and incomplete history are marked `complete=false`. Older reports may omit timing. The Hub stores timing only with accepted reports and adds its own `received_at`; stale/replayed reports cannot alter it. A newer accepted report without timing clears the stored snapshot. Browser estimates advance from that controller timestamp, avoiding worker/browser clock offsets, and terminal durations stay fixed. CSV `timing.*` columns contain worker snapshots, with `observed_at` identifying when running durations were sampled. Historical jobs without timing remain unknown rather than inferred from submission or last-update times.

## Validation

### Matrix, allocation and project lifecycle additions

Task `scheduling={mode:auto|assisted|manual,node_ids:[],preferred_node_ids:[],gpu_uuids:[]}` adds hard candidate restrictions, ordered preferences, and optional hard GPU restrictions. Manual mode requires exactly one node. Priority remains -100..100 and orders unassigned work; it does not preempt running jobs. GPU restrictions require worker capability `scheduler-v2`. Target deployment templates resolve node-specific source/environment identity for the same project bundle and experiment preset. Assignment and preview use the same matching logic; offline ownership never expires.

Admin matrix endpoints: GET `/api/matrices`, GET `/api/matrices/item?id=...`, POST `/api/matrices/save`, POST `/api/matrices/preview`, POST `/api/matrices/start`, POST `/api/matrices/delete`, GET `/api/matrices/report.md?id=...`. A definition contains name, description, spec, datasets `[{name,params,assets?}]`, and grid. Dataset variants and parameter grid form a Cartesian product, max256. Save revisions protect against stale edits. Start `{id,revision,request_id}` atomically creates a persisted run and immutable jobs; retries of the same request return the same IDs. Removing a matrix retains historical jobs and reports. Markdown reports distinguish missing, incomplete, failed and completed results without combining incomparable metric protocols.

Admin POST `/api/projects/delete {digest,retry?}` hides the bundle and records durable per-node deletion revisions. GET state includes `project_deletions` for cleanup progress. The authenticated worker sync carries delete declarations only to `project-delete-v1` workers. Only worker-owned receipts, deterministic storage paths and Docker ownership labels determine cleanup targets; controller requests do not supply arbitrary paths or image names. Active jobs/pending commands prevent deletion; deleted projects cannot resume. Source originals, archived results and shared dependencies remain. Controller package cleanup and worker cleanup survive retries/restarts; failed cleanup remains visible.

Admin GET/POST `/api/ai/settings` configures opt-in server-side Chat Completions assistance; API keys never appear in returned settings, state or worker config. POST `/api/ai/test` tests the saved service. Loopback-admin POST `/api/local/imports/assist` returns validated draft suggestions, summary, warnings and field differences without persisting or executing them. Current configuration and supplied logs are sent only on explicit use; entry-source inclusion requires `include_source:true` and is bounded to 32KB. Source/asset locations and native resume capability cannot be changed by AI. POST `/api/local/imports/test-metrics` tests rules against sample logs with actual Python regex in a bounded subprocess. API base URLs require HTTPS except local loopback services; redirects are refused to avoid forwarding credentials.

Tests cover lost sync ACK, duplicate POST, node ownership/auth, unsafe paths, upload interruption/resume/hash failure, offline job ownership, device admission/exclusive policy, cached tasks progressing offline, agent restart not double-launching, cooperative stop/resume and records. Release tests additionally cover pairing, runtime selection, project registration, role-specific ZIP contents and file manifests. Hardware acceptance is separate from unit tests; see the release notes for validation scope. Demo must actually work end-to-end without installation.

## Release setup

The controller exports a per-worker pairing file. The Linux/WSL worker launcher authenticates it, discovers GPUs and the local Docker endpoint, builds a pinned runtime, verifies each enabled UUID with a CUDA calculation, and writes its own node configuration. Setup is separate from the agent loop; existing configured workers may restart while the controller is temporarily offline. Foreground launchers do not install a system service.

Node snapshots may include task_templates: up to 100 validated task specifications. They are admin-visible suggestions; selecting one fills the browser editor and never submits a job automatically. The project wizard registers a clean, committed local Git repository and optional dataset path without modifying the algorithm.

## SASRec external adapter and diagnostics (2026-09-12)

Dedicated subprocess command: python -m expman.adapters.sasrec --project /workspace/code/SASRec_Original. It uses the existing Run environment. Flat params comprise the original _DEFAULTS plus dataset/data_asset/torch_threads; unknown params fail, device is explicit cpu or cuda and gpu_id=0. The Docker image embeds expman under /opt/experiment-manager; algorithm source comes from the pinned task Git commit.

Protocol: schema3 / external_sasrec_original_v1, process-local OUTPUT_ROOT binding, post-epoch _append_log callback. No changes to original source files. Complete epoch => immutable ZIP(config,last,optionalbest,member SHA256 manifest), SDK publication, metric and STOP. Resume verifies source/data/runtime/params and the entire ZIP before restoring working files. Same task paths and runtime required; no cross-node migration. Older bundles removed only after publication. Epoch100 raw checkpoint remains a separate reusable result.

Opt-in python -m expman sasrec-smoke --project PATH --root NEW_PATH --device cpu|cuda generates synthetic data and compares continuous vs resumed actual SASRec model/Adam/RNG/earlystop/test metrics. Does not install dependencies; requires torch/numpy. Core tests need no algorithm package; EXPMAN_SASREC_PROJECT enables additional real subprocess tests.

doctor calls diagnostics.inspect_node. Output: ready/readiness_scope=local_preflight/gpu_runtime_validated=false/checks/snapshot. Nonready CLI exitcode2. Read-only local checks, no Hub access/imagepull/containerstart/install/configuration mutation. Passing checks are not proof of algorithm/GPU compatibility.
