/**
 * Feature flags for panels that belong to later phases. The tyre panel shipped in Phase 6F and is
 * on unless NEXT_PUBLIC_FEATURE_TIRES=0. The AI engineer is off until its phase ships
 * (NEXT_PUBLIC_FEATURE_ENGINEER=1), so its card renders the "coming in a later phase" state.
 */
export const features = {
  tires: process.env.NEXT_PUBLIC_FEATURE_TIRES !== "0",
  engineer: process.env.NEXT_PUBLIC_FEATURE_ENGINEER === "1",
} as const;

export type FeatureName = keyof typeof features;
