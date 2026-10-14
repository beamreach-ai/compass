import WorkspaceShell from '../components/WorkspaceShell';

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const appMode = process.env.APP_MODE ?? 'production';
  const productionUrl = process.env.PRODUCTION_URL ?? '';
  const showDemoBanner = appMode === 'demo' && productionUrl;

  return (
    <div className="min-h-screen">
      <WorkspaceShell>
          {showDemoBanner ? (
            <div className="px-4 pt-4 md:px-8 md:pt-6">
              <div className="rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
                Previewing the Beamreach demo workspace.
                {' '}
                <a href={productionUrl} className="font-semibold underline underline-offset-2">
                  Deploy to your AWS account
                </a>
                .
              </div>
            </div>
          ) : null}
          <main>{children}</main>
      </WorkspaceShell>
    </div>
  );
}
