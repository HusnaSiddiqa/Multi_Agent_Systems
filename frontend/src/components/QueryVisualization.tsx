/**
 * Wrapper for GenieQueryVisualization from @databricks/appkit-ui
 * Error boundary prevents a bad viz from crashing the whole thread.
 */

import { GenieQueryVisualization } from "@databricks/appkit-ui/react";
import { ErrorBoundary } from "./ErrorBoundary";
import type { StatementResponse } from "../types";

interface Props {
  data: StatementResponse;
}

export function QueryVisualization({ data }: Props) {
  if (
    !data?.manifest?.schema?.columns?.length ||
    !data?.result?.data_array?.length
  ) {
    return (
      <div className="viz-empty">
        <p>No data to display</p>
      </div>
    );
  }

  return (
    <ErrorBoundary>
      <div className="viz-container">
        <GenieQueryVisualization data={data} />
      </div>
    </ErrorBoundary>
  );
}
