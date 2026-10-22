// One fava instance for one ledger, behind Cloudflare Access (configured in the
// dashboard -- fava itself has no login).  The container clones the ledger on
// every cold start and re-fetches it every 5 minutes.
import { Container, getContainer } from "@cloudflare/containers";

interface Env {
	FAVA: DurableObjectNamespace<Fava>;
	LEDGER_REPO: string;   // var: <owner>/<repo>
	LEDGER_BRANCH: string; // var
	GITHUB_TOKEN: string;  // secret: read-only token for LEDGER_REPO
}

export class Fava extends Container<Env> {
	defaultPort = 5000;
	sleepAfter = "30m";

	constructor(ctx: DurableObjectState, env: Env) {
		super(ctx, env);
		this.envVars = {
			LEDGER_REPO: env.LEDGER_REPO,
			LEDGER_BRANCH: env.LEDGER_BRANCH,
			GITHUB_TOKEN: env.GITHUB_TOKEN,
		};
	}
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		return getContainer(env.FAVA, "ledger").fetch(request);
	},
};
