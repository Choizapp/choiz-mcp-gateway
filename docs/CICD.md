# CI/CD setup

Builds run on GitHub Actions, images are pushed to GHCR (GitHub Container Registry), and deploys to the EC2 happen via AWS SSM Run Command. The EC2 never builds anything — it pulls finished images.

## What runs where

| Trigger | What happens |
|---|---|
| Push to `master` touching `gateway/**`, `mcp/**`, or `compose.yml` | `.github/workflows/deploy-gateway.yml` builds only the changed images for `linux/arm64` (EC2 is Graviton), pushes them to GHCR, then sends an SSM command to the EC2 that base64-pushes the new `compose.yml` inline and runs `docker compose pull && up -d` |
| Push to `master` touching `worker/**` | `.github/workflows/deploy-worker.yml` runs `wrangler deploy` |
| Manual dispatch | Both workflows expose `workflow_dispatch` for forced rebuilds/deploys |

The deploy step does **not** run `git pull` on the EC2 — `compose.yml` is shipped over the SSM payload itself, so the EC2 needs no GitHub credentials and the file always matches the deployed commit.

Image naming on GHCR:

- `ghcr.io/choizapp/choiz-mcp-gateway/gateway:<tag>`
- `ghcr.io/choizapp/choiz-mcp-gateway/warehouse:<tag>`
- `ghcr.io/choizapp/choiz-mcp-gateway/meta-ads:<tag>`

Each image is tagged twice on every successful build: `:latest` and `:<commit-sha>`. Production reads `:latest`. To roll back, set `IMAGE_TAG=<sha>` in the EC2 `.env` and run `docker compose up -d`.

## One-time setup

### 1. AWS — IAM OIDC provider for GitHub

In the AWS console (region doesn't matter for IAM):

1. **IAM → Identity providers → Add provider**
   - Provider type: **OpenID Connect**
   - Provider URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`
   - Click **Get thumbprint**, then **Add provider**.

If a provider for `token.actions.githubusercontent.com` already exists in this AWS account, skip — you can reuse it.

### 2. AWS — IAM role `choiz-mcp-gateway-ci`

1. **IAM → Roles → Create role**
   - Trusted entity type: **Web identity**
   - Identity provider: `token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`
   - GitHub organization: `Choizapp`
   - GitHub repository: `choiz-mcp-gateway`
   - GitHub branch: `master`
   - Next.

2. Skip the AWS managed policies (we'll add an inline one). Next, name the role `choiz-mcp-gateway-ci`, create.

3. Open the role → **Add permissions → Create inline policy** → JSON:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "SendDeployCommand",
         "Effect": "Allow",
         "Action": "ssm:SendCommand",
         "Resource": [
           "arn:aws:ssm:*:*:document/AWS-RunShellScript",
           "arn:aws:ec2:<REGION>:<ACCOUNT_ID>:instance/<INSTANCE_ID>"
         ]
       },
       {
         "Sid": "ReadCommandResult",
         "Effect": "Allow",
         "Action": [
           "ssm:GetCommandInvocation",
           "ssm:ListCommandInvocations"
         ],
         "Resource": "*"
       }
     ]
   }
   ```

   Replace `<REGION>`, `<ACCOUNT_ID>`, `<INSTANCE_ID>` with the actual values. Save the policy with name `deploy-via-ssm`.

4. Tighten the **trust policy** (Trust relationships tab → Edit) to lock it to the master branch:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": {
           "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
         },
         "Action": "sts:AssumeRoleWithWebIdentity",
         "Condition": {
           "StringEquals": {
             "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
           },
           "StringLike": {
             "token.actions.githubusercontent.com:sub": "repo:Choizapp/choiz-mcp-gateway:ref:refs/heads/master"
           }
         }
       }
     ]
   }
   ```

5. Copy the **role ARN** — you'll paste it into GitHub as `AWS_ROLE_ARN`.

#### Optional: Cost Explorer read-only

To enable the `cost-report.yml` workflow (Cost Explorer queries from CI), add a second inline policy on the same role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CostExplorerRead",
      "Effect": "Allow",
      "Action": [
        "ce:GetCostAndUsage",
        "ce:GetCostAndUsageWithResources",
        "ce:GetCostForecast",
        "ce:GetDimensionValues",
        "ce:GetTags",
        "ce:GetCostCategories",
        "ce:GetRightsizingRecommendation",
        "ce:GetSavingsPlansPurchaseRecommendation",
        "ce:GetReservationPurchaseRecommendation",
        "ce:GetAnomalies",
        "ce:GetAnomalyMonitors",
        "ce:GetAnomalySubscriptions",
        "ce:DescribeReport",
        "ce:DescribeNotificationSubscription",
        "compute-optimizer:GetEC2InstanceRecommendations",
        "compute-optimizer:GetEBSVolumeRecommendations",
        "compute-optimizer:GetRecommendationSummaries",
        "compute-optimizer:GetEnrollmentStatus"
      ],
      "Resource": "*"
    }
  ]
}
```

Save as `cost-explorer-read`. All actions are read-only. Each `ce:*` call costs $0.01 against the AWS billing API; typical run is one request.

If Compute Optimizer was never enabled in the account, the `compute-optimizer:*` calls return an `OptInRequired` error. Enable it once at **Compute Optimizer console → Get started → Opt in** (free).

### 3. GitHub — secrets and variables

**Settings → Secrets and variables → Actions** on `Choizapp/choiz-mcp-gateway`.

**Repository secrets:**

| Name | Value |
|---|---|
| `AWS_ROLE_ARN` | The ARN from step 2.5 |
| `AWS_REGION` | The region of the EC2 (e.g. `us-east-1`) |
| `EC2_INSTANCE_ID` | The `i-xxxxxxxxx` of the gateway EC2 |
| `CLOUDFLARE_API_TOKEN` | Cloudflare API token with `Workers Scripts:Edit` and `Workers KV Storage:Edit` on the `choiz.com.mx` zone (mint at https://dash.cloudflare.com/profile/api-tokens) |

**Repository variables:**

| Name | Value |
|---|---|
| `EC2_REPO_PATH` | Absolute path of the repo on the EC2 (e.g. `/home/ssm-user/choiz-mcp-gateway`) |

### 4. EC2 — log in to GHCR so `docker compose pull` works

GHCR images for this repo are private. The EC2 needs credentials to pull them.

Mint a fine-grained personal access token (or classic PAT) with `read:packages` scope at https://github.com/settings/tokens. (A fine-grained token scoped to the org's `choiz-mcp-gateway` package is preferable; classic PAT works as a fallback.) Then via SSM Session Manager on the EC2:

```bash
# Replace <USERNAME> with your GitHub username and paste the PAT when prompted.
echo <PAT> | sudo docker login ghcr.io -u <USERNAME> --password-stdin
```

Verify:

```bash
sudo cat /root/.docker/config.json
# Should contain an "auths" entry for ghcr.io
```

`sudo` matters — SSM Run Command runs as root, so the credentials must be in root's Docker config, not `ssm-user`'s.

### 5. EC2 — make sure the target directory exists

The deploy script does `mkdir -p $EC2_REPO_PATH` before writing `compose.yml`, so the path doesn't have to pre-exist — but it does need to be the same path the `.env` file lives in (since `docker compose` reads both from the same dir). Confirm:

```bash
ls /home/ssm-user/choiz-mcp-gateway/.env
```

If `.env` lives elsewhere, set `EC2_REPO_PATH` to that directory.

## How to deploy day-to-day

```bash
# from your laptop
git push origin master
```

That's it. Watch the run at https://github.com/Choizapp/choiz-mcp-gateway/actions.

To force a rebuild without code changes (e.g. after rotating a base image): GitHub → Actions → "Build and deploy gateway stack" → Run workflow.

## Rolling back

The workflow tags every image with the commit SHA. To pin the EC2 to an older image without reverting code:

```bash
# on EC2
echo "IMAGE_TAG=<old-sha>" >> /home/ssm-user/choiz-mcp-gateway/.env
cd /home/ssm-user/choiz-mcp-gateway && docker compose up -d
```

To return to "always run latest", remove the `IMAGE_TAG` line from `.env` and `docker compose up -d` again.

## Local development

To rebuild images locally without going through CI (e.g. when testing a Dockerfile change):

```bash
docker compose -f compose.yml -f compose.dev.yml up --build
```

`compose.dev.yml` re-adds the `build:` directives that production no longer uses.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Workflow fails at `Configure AWS credentials` with `AccessDenied` | Trust policy `sub` claim does not match `repo:Choizapp/choiz-mcp-gateway:ref:refs/heads/master`. Check the OIDC token claims in the failed run's log |
| Workflow fails at `wrangler deploy` with 401/403 | `CLOUDFLARE_API_TOKEN` is missing the right scopes or the wrong zone |
| `docker compose pull` fails on EC2 with `unauthorized` | PAT expired or `docker login ghcr.io` was not done as root. Re-run step 4 |
| `docker compose pull` fails with `no matching manifest for linux/arm64/v8` | An image was built without `platforms: linux/arm64`. Check the build job in `deploy-gateway.yml` |
| SSM step times out | Instance is not reachable by SSM agent. Reboot the instance from AWS Console; verify SSM agent is running with `systemctl status amazon-ssm-agent` |
| Workflow runs but no images get rebuilt and deploy is skipped | Expected when only the workflow file or unrelated docs changed. To force a rebuild, use **Run workflow** (workflow_dispatch) on the Actions tab |
