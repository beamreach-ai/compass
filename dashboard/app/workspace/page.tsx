import { Suspense } from 'react';
import DashboardClient from '../components/DashboardClient';

export default function AppPage() {
  return (
    <Suspense fallback={<div className="p-8">Loading...</div>}>
      <DashboardClient />
    </Suspense>
  );
}
