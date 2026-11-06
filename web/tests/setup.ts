import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// jsdom has no layout, and Framer Motion asks for one when animating height.
window.scrollTo = () => undefined;

afterEach(cleanup);
