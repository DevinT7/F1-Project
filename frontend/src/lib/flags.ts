/**
 * Feature flags for panels that belong to later phases. Both default to off, so their cards
 * render the "coming in a later phase" state. Set NEXT_PUBLIC_FEATURE_TIRES=1 /
 * NEXT_PUBLIC_FEATURE_ENGINEER=1 once those phases ship real components.
 */
export const features = {
  tires: process.env.NEXT_PUBLIC_FEATURE_TIRES === "1",
  engineer: process.env.NEXT_PUBLIC_FEATURE_ENGINEER === "1",
} as const;

export type FeatureName = keyof typeof features;
