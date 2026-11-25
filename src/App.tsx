/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export default function App() {
  return (
    <div className="w-screen h-dvh overflow-hidden bg-[#f1f3f6] dark:bg-[#16181c]">
      <iframe
        src="/geoquiz.html"
        title="GeoQuiz"
        className="w-full h-full border-0 block"
        allow="clipboard-write"
      />
    </div>
  );
}
