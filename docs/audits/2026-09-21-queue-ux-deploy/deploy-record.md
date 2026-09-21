# Deploy record — 2026-09-21 assessment queue/headline change

Commits 9fcea41, a9c206e, 7f546c9, 8693dac on `blackbird`. No migration (head 0049 before and after).

## Additional evidence collected during the deploy but missing from the first dump

### Disk headroom, checked BEFORE the build
```
Filesystem      Size  Used Avail Use% Mounted on
/dev/root        61G   29G   33G  47% /
Images 36 / 11.07GB, Build Cache 9.996GB (measured 18:35Z, pre-build)
```
Recorded because this filesystem is shared with org1 and prior audits measured it at 92-96%.

### copi-edge membership (the shared network), enumerated
```
copi-python-nginx-1=172.19.0.3/16 copi-blackbird-app-1=172.19.0.2/16 
```
Exactly two members. No container named or aliased `postgres` is on copi-edge, which is what makes the bare `postgres` hostname in DATABASE_URL safe for a one-off container attached to it.

### blackbird-app copi-edge address, before and after
before: 172.19.0.2 (18:35Z)  after: 172.19.0.2 (18:39Z) — unchanged, so org1 nginx needed no reload.
NOT guaranteed: the static `upstream blackbird_app { server blackbird-app:8000; }` in org1 nginx resolves once at config load. Compose removes the old container before creating the new one and copi-edge has only two members, so .2 is the lowest free address and is reused. A third member taking .2 in that window would leave blackbird.copi.science 502ing until `docker exec copi-python-nginx-1 nginx -s reload`.

### Rollback artifacts (cut AFTER the deploy, in response to the audit)
```
copi-blackbird-blackbird-app:rollback-6bef292 471422040a89
copi-blackbird-worker:rollback-6bef292 776bfd680682
copi-blackbird-agent:rollback-6bef292 fa099e0ff800
rollback-6bef292
```
Attributed by each image's own com.docker.compose.service label, not by timestamp guesswork.

## Original post-deploy dump

# Deploy evidence, 2026-09-21 (collected post-deploy)
## git
8693dac docs(assessments): design, plan and deploy box for the 2026-09-21 queue change
7f546c9 feat(prompts): headline must name the disease and what the intervention does
a9c206e feat(assessments): queue sub-tabs, collapsed score, cited-paper links
9fcea41 feat(assessments): cited-paper link helpers for assessment prose
6bef292 fix(assessments): readability pass from the 2026-09-15 visual audit

 M docker-compose.prod.yml
?? .deploy_evidence.md

## alembic (new image vs live DB)
INFO  [alembic.runtime.migration] Will assume transactional DDL.
0049 (head)

## containers (all, both projects)
copi-blackbird-agent-1	running	copi-blackbird	agent	3 minutes ago
copi-blackbird-app-1	running	copi-blackbird	blackbird-app	3 minutes ago
copi-blackbird-postgres-1	running	copi-blackbird	postgres	8 weeks ago
copi-blackbird-worker-1	running	copi-blackbird	worker	3 minutes ago
agent-run	running	copi-python	agent	4 days ago
copi-python-app-1	running	copi-python	app	4 days ago
copi-python-certbot-1	running	copi-python	certbot	4 days ago
copi-python-grantbot-1	running	copi-python	grantbot	4 days ago
copi-python-migrate-1	exited	copi-python	migrate	4 days ago
copi-python-nginx-1	running	copi-python	nginx	6 weeks ago
copi-python-postgres-1	running	copi-python	postgres	4 days ago
copi-python-worker-1	running	copi-python	worker	4 days ago

## blackbird build_info
app: {"commit":"8693dac43a3f5a390578874c4a7446b0b7beaec0","branch":"blackbird","dirty_files":1,"generated_at":"2026-09-21T18:37:25.410480+00:00","generator":"scripts/write_build_info.py"}
worker: {"commit":"8693dac43a3f5a390578874c4a7446b0b7beaec0","branch":"blackbird","dirty_files":1,"generated_at":"2026-09-21T18:37:25.410480+00:00","generator":"scripts/write_build_info.py"}
agent: {"commit":"8693dac43a3f5a390578874c4a7446b0b7beaec0","branch":"blackbird","dirty_files":1,"generated_at":"2026-09-21T18:37:25.410480+00:00","generator":"scripts/write_build_info.py"}

## image ids in use
copi-blackbird-app-1       f38dee6f57c2
copi-blackbird-worker-1    cfdc3804fd11
copi-blackbird-agent-1     8b8a1395876e

## networks of blackbird-app
copi-blackbird_default=172.20.0.4 aliases=[copi-blackbird-app-1 blackbird-app] copi-edge=172.19.0.2 aliases=[copi-blackbird-app-1 blackbird-app] 

## host port bindings, blackbird containers
copi-blackbird-app-1       {"8000/tcp":null}
copi-blackbird-worker-1    {"8000/tcp":null}
copi-blackbird-agent-1     {"8000/tcp":null}
copi-blackbird-postgres-1  {"5432/tcp":null}

## supervisor + schema + data sentinels
1|idle|00:00:03.178193
0049
22|1841|79|2|19

## org1 StartedAt (baseline vs now: baseline captured 18:35:31Z)
copi-python-app-1          2026-09-17T02:17:16.805683092Z
copi-python-nginx-1        2026-08-06T21:55:15.97091851Z
copi-python-postgres-1     2026-09-17T02:15:39.953088169Z
copi-python-worker-1       2026-09-17T02:17:16.813536332Z
copi-python-grantbot-1     2026-09-17T02:17:16.810190792Z
copi-python-certbot-1      2026-09-17T02:21:38.327521805Z
agent-run                  2026-09-17T02:22:30.777028775Z

## org1 sentinel + http
145
copi.science=200
blackbird=302

## backup
-rw-rw-r-- 1 ubuntu ubuntu 127691006 Sep 21 18:34 /home/ubuntu/backups-blackbird/pre_deploy_queue_ux_20260921T183435Z.dump

## prompt version in the agent bind mount
version = "1.6.0"
