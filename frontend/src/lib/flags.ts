/**
 * Feature flags for panels. The tyre panel shipped in Phase 6F and the AI engineer in Phase 7F;
 * each is on unless NEXT_PUBLIC_FEATURE_TIRES=0 / NEXT_PUBLIC_FEATURE_ENGINEER=0. (The server-side
 * LLM kill switch is ENGINEER_LLM_ENABLED: with it off the card still shows the template report.)
 */
export const features = {
  tires: process.env.NEXT_PUBLIC_FEATURE_TIRES !== "0",
  engineer: process.env.NEXT_PUBLIC_FEATURE_ENGINEER !== "0",
} as const;

export type FeatureName = keyof typeof features;
