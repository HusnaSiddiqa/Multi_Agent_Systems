import { Spinner } from "./Spinner";
/**
 * Live processing progress steps
 *
 * The last step in the array is treated as the *active* (in-progress) step —
 * it gets a spinning indicator. All preceding steps are rendered as completed
 * with a checkmark. When `isLoading` turns false in the parent, this entire
 * component unmounts, so we never need a "final done" state here.
 */

interface Step {
  name: string;
  details?: string;
}

interface Props {
  steps: Step[];
  path?: "click" | "semantic";
}

export function ProgressSteps({ steps }: Props) {
  if (steps.length === 0) {
    // No steps yet — show a simple "starting" spinner
    return (
      <div className="progress-container">
        <div className="progress-step active">
          <Spinner size={16} />
          <span className="step-label">Starting…</span>
        </div>
      </div>
    );
  }

  const completedSteps = steps.slice(0, -1);
  const activeStep = steps[steps.length - 1];

  return (
    <div className="progress-container">
      {completedSteps.map((step, i) => (
        <div key={i} className="progress-step done">
          <span className="step-check">{"✓"}</span>
          <span className="step-label">{step.name}</span>
          {step.details && (
            <span className="step-detail">{step.details}</span>
          )}
        </div>
      ))}
      <div className="progress-step active">
        <Spinner size={16} />
        <span className="step-label">{activeStep.name}</span>
        {activeStep.details && (
          <span className="step-detail">{activeStep.details}</span>
        )}
      </div>
    </div>
  );
}
