import { redirect } from 'next/navigation';

const GOOGLE_LOGO = (
  <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5">
    <path
      fill="#EA4335"
      d="M12 10.2v3.9h5.5c-.2 1.3-1.5 3.9-5.5 3.9-3.3 0-6-2.7-6-6s2.7-6 6-6c1.9 0 3.1.8 3.8 1.5l2.6-2.5C16.8 3.4 14.7 2.5 12 2.5 6.8 2.5 2.5 6.8 2.5 12S6.8 21.5 12 21.5c6.3 0 9.4-4.4 9.4-6.7 0-.5-.1-.9-.1-1.2H12Z"
    />
    <path
      fill="#FBBC05"
      d="M3.6 7.4 6.8 9.7C7.7 7.5 9.7 6 12 6c1.9 0 3.1.8 3.8 1.5l2.6-2.5C16.8 3.4 14.7 2.5 12 2.5c-3.7 0-6.8 2.1-8.4 4.9Z"
    />
    <path
      fill="#34A853"
      d="M12 21.5c2.6 0 4.8-.9 6.5-2.5l-3-2.4c-.8.6-1.9 1.1-3.5 1.1-3.9 0-5.3-2.6-5.5-3.9l-3.2 2.5C4.9 19.3 8.1 21.5 12 21.5Z"
    />
    <path
      fill="#4285F4"
      d="M21.4 10.8H12v3.9h5.5c-.3 1.4-1.1 2.4-2 3l3 2.4c1.8-1.7 2.9-4.2 2.9-7.4 0-.6-.1-1.2-.2-1.9Z"
    />
  </svg>
);

export const dynamic = 'force-dynamic';

export default function LandingPage() {
  const appMode = process.env.APP_MODE ?? "production";
  const productionUrl = process.env.PRODUCTION_URL ?? "";
  const isDemo = appMode === "demo";
  const cognitoDomain = process.env.COGNITO_USER_POOL_DOMAIN ?? '';
  const clientId = process.env.COGNITO_USER_POOL_CLIENT_ID_ALB ?? '';
  const redirectUri = process.env.AUTH_REDIRECT_URI ?? '';
  const authConfigured = Boolean(cognitoDomain && clientId && redirectUri);

  // Self-hosted without Cognito: use the simple username/password login page instead.
  if (!authConfigured && !isDemo) {
    redirect('/login');
  }
  const encodedRedirectUri = encodeURIComponent(redirectUri);
  const baseQuery = `client_id=${clientId}&response_type=code&scope=openid+email+profile&redirect_uri=${encodedRedirectUri}`;
  const googleHref = authConfigured ? '/google-login' : '#';
  const emailSignInHref = authConfigured ? `https://${cognitoDomain}/login?${baseQuery}` : '#';
  const registerHref = isDemo && productionUrl
    ? productionUrl
    : authConfigured
      ? `https://${cognitoDomain}/signup?${baseQuery}`
      : '#';

  return (
    <div className="flex min-h-screen items-center justify-center px-6 py-16">
      <div className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-8 shadow-sm">
        <div className="mb-8 text-center">
          <div className="mb-4 flex items-center justify-center gap-3">
            <img
              src="https://s3.us-east-1.amazonaws.com/beamreach.cloud/assets/images/beamreach-logo.png"
              alt="Beamreach"
              className="h-10 w-auto"
            />
            <div className="text-left">
              <div className="text-lg font-semibold text-gray-900">Beamreach</div>
              <div className="text-lg font-semibold text-gray-900">Compass</div>
            </div>
          </div>
          <h1 className="text-2xl font-semibold text-gray-900">
            {isDemo ? "Explore the demo workspace" : "Sign in to your workspace"}
          </h1>
          <p className="mt-2 text-sm text-gray-600">
            {isDemo
              ? "Use Google to open the shared demo account: a synthetic map beside a real one, decoys you can trip, pull requests that are never applied. It resets on Sunday."
              : "Use Google for one-click access, or continue with email and password through Cognito."}
          </p>
        </div>

        <div className="space-y-3">
          <a
            href={googleHref}
            aria-disabled={!authConfigured}
            className={`flex w-full items-center justify-center gap-3 rounded-lg border px-4 py-3 text-sm font-medium transition ${
              authConfigured
                ? 'border-gray-300 bg-white text-gray-900 hover:bg-gray-50'
                : 'cursor-not-allowed border-gray-200 bg-gray-100 text-gray-400'
            }`}
          >
            {GOOGLE_LOGO}
            <span>Continue with Google</span>
          </a>

          <a
            href={emailSignInHref}
            aria-disabled={!authConfigured}
            className={`flex w-full items-center justify-center rounded-lg px-4 py-3 text-sm font-medium transition ${
              authConfigured
                ? 'bg-gray-900 text-white hover:bg-gray-800'
                : 'cursor-not-allowed bg-gray-300 text-white'
            }`}
          >
            Sign in with email
          </a>

          <a
            href={registerHref}
            aria-disabled={isDemo ? !productionUrl : !authConfigured}
            className={`flex w-full items-center justify-center rounded-lg border px-4 py-3 text-sm font-medium transition ${
              (isDemo ? Boolean(productionUrl) : authConfigured)
                ? 'border-gray-300 text-gray-700 hover:bg-gray-50'
                : 'cursor-not-allowed border-gray-200 text-gray-400'
            }`}
          >
            {isDemo ? 'Run it yourself' : 'Register'}
          </a>
        </div>

        <p className="mt-6 text-center text-xs text-gray-500">
          {authConfigured
            ? isDemo
              ? 'The demo account is shared with every visitor and resets on Sunday.'
              : 'After authentication, access is still limited by Beamreach workspace membership.'
            : 'Authentication is not configured for this environment yet.'}
        </p>
      </div>
    </div>
  );
}
