# Bootstrap

One-time AWS setup for continuous deployment. Everything in this folder is deployed by hand once per account; after that, merging to `main` deploys the application automatically.

## What's here

- **`github-oidc.yaml`** — CloudFormation template that creates:
  - The GitHub OIDC identity provider (one per AWS account).
  - An IAM role `hospitality-hq-github-deploy` that GitHub Actions assumes to run `sam deploy`.
  - A trust policy scoped to `erachi/hospitality-hq` on `refs/heads/main` only — no other repo or branch can assume this role.

## One-time deploy

```bash
aws cloudformation deploy \
  --stack-name hospitality-hq-github-oidc \
  --template-file bootstrap/github-oidc.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GithubRepo=erachi/hospitality-hq
```

If the GitHub OIDC provider already exists in your account (another repo created it), add `CreateOIDCProvider=false` to the parameter overrides.

After the stack is created:

1. Get the deploy role ARN:
   ```bash
   aws cloudformation describe-stacks \
     --stack-name hospitality-hq-github-oidc \
     --query 'Stacks[0].Outputs[?OutputKey==`DeployRoleArn`].OutputValue' \
     --output text
   ```
2. In GitHub: **Settings → Secrets and variables → Actions → Variables** → add a new repository variable:
   - Name: `AWS_DEPLOY_ROLE_ARN`
   - Value: the ARN from step 1
3. Variables (not secrets) are fine — ARNs are not sensitive.

## Permissions model

The deploy role has `PowerUserAccess` plus scoped IAM permissions for roles named `hospitality-hq-*`. This is broad but the trust policy is narrow: only `erachi/hospitality-hq`'s `main` branch, as validated by GitHub's OIDC token, can assume this role. No long-lived AWS keys exist for this path.

Tighten later if you want (scope service permissions to specific resource ARNs), but the current shape matches what AWS recommends for small-team SAM deployments.

## Rotating / revoking

- To revoke CD: delete the CloudFormation stack, or revoke the trust by editing the role's trust policy in the AWS Console.
- To change the trusted branch: update the `GithubBranch` parameter and re-run the deploy command.
