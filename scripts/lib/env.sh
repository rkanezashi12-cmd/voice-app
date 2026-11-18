# shellcheck shell=bash
# 接続先の GCP（プロジェクト・リージョン・サービス名）を deploy/ops.env から読む（scripts/*.sh が source する）。
# 環境変数が先に入っていれば、そちらを使う（GitHub Actions の ops は deploy/ops.env を環境変数に入れてから動く）。
# ★既定のプロジェクトは持たない：deploy/ops.env が無い・空なら止まる（メンバーの複製が、ほかの人の GCP に繋がらないように）。
_voice_app_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_voice_app_env="${OPS_ENV:-${_voice_app_root}/deploy/ops.env}"
if [ -f "$_voice_app_env" ]; then
  while IFS='=' read -r _k _v; do
    case "$_k" in '' | \#*) continue ;; esac
    [[ "$_k" =~ ^[A-Z][A-Z0-9_]*$ ]] || continue
    if [ -z "${!_k:-}" ]; then
      export "$_k=$_v"
    fi
  done <"$_voice_app_env"
fi
PROJECT="${PROJECT:-${GCP_PROJECT_ID:-}}"
if [ -z "$PROJECT" ]; then
  printf 'エラー: GCP のプロジェクトが決まっていません（deploy/ops.env の GCP_PROJECT_ID を入れてください。docs/new-environment.md）\n' >&2
  exit 1
fi
REGION="${REGION:-asia-northeast1}"
SERVICE="${SERVICE:-meeting-notes}"
RUN_SA="${RUN_SA:-${SERVICE}-run@${PROJECT}.iam.gserviceaccount.com}"
TASKS_SA="${TASKS_SA:-${SERVICE}-tasks@${PROJECT}.iam.gserviceaccount.com}"
export PROJECT REGION SERVICE RUN_SA TASKS_SA
unset _voice_app_root _voice_app_env _k _v
