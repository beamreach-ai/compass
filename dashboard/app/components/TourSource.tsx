"use client";

import { useEffect } from "react";

import { captureTourSource } from "../lib/tour";

// Keeps the landing's `?story=`, `?src=` and `?gclid=` for the session, on whichever page the
// visitor lands on: the sign-in round trip through Cognito drops the query string, the tour
// panel on /workspace reads them back. Renders nothing; outside demo mode it is a no-op in
// effect, since nothing reads the stored value.
export default function TourSource() {
  useEffect(() => {
    captureTourSource();
  }, []);
  return null;
}
