// TCP echo server for the RTT probe. It runs in the database network namespace.
import net from "node:net";

const server = net.createServer((socket) => {
  socket.setNoDelay(true);
  socket.on("data", (chunk) => socket.write(chunk));
  socket.on("error", () => socket.destroy());
});
server.maxConnections = 16;
server.listen(7777, "0.0.0.0");
