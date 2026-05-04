package main

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log"
	"time"

	libp2p "github.com/libp2p/go-libp2p"
	pubsub "github.com/libp2p/go-libp2p-pubsub"
	crypto "github.com/libp2p/go-libp2p/core/crypto"
	host "github.com/libp2p/go-libp2p/core/host"
	peer "github.com/libp2p/go-libp2p/core/peer"
	multiaddr "github.com/multiformats/go-multiaddr"
)

const TopicName = "kernell-gossip-v1"

type ProtocolMessage struct {
	MsgID     string `json:"msg_id"`
	Epoch     int64  `json:"epoch"`
	Type      string `json:"type"`
	Payload   string `json:"payload"`
	Signature string `json:"signature"`
}

// 🔐 Simulación de validación (esto luego será gRPC hacia Python)
func validateMessage(msg ProtocolMessage) bool {
	// Aquí conectas con tu validator real (Python)
	// Por ahora: reglas mínimas
	if msg.MsgID == "" || msg.Type == "" {
		return false
	}

	// Rechazar mensajes del futuro
	nowEpoch := time.Now().Unix() / 5
	if msg.Epoch > nowEpoch+1 {
		return false
	}

	return true
}

func createHost() host.Host {
	priv, _, err := crypto.GenerateEd25519Key(rand.Reader)
	if err != nil {
		panic(err)
	}

	h, err := libp2p.New(
		libp2p.Identity(priv),
	)
	if err != nil {
		panic(err)
	}

	return h
}

func connectToPeer(ctx context.Context, h host.Host, addr string) {
	maddr, err := multiaddr.NewMultiaddr(addr)
	if err != nil {
		log.Println("Invalid multiaddr:", err)
		return
	}

	info, err := peer.AddrInfoFromP2pAddr(maddr)
	if err != nil {
		log.Println("Invalid peer info:", err)
		return
	}

	if err := h.Connect(ctx, *info); err != nil {
		log.Println("Connection failed:", err)
	} else {
		log.Println("Connected to:", info.ID)
	}
}

func main() {
	ctx := context.Background()

	// 🧠 Crear nodo
	h := createHost()
	log.Println("Node ID:", h.ID().String())

	for _, addr := range h.Addrs() {
		fmt.Printf("Listen: %s/p2p/%s\n", addr, h.ID())
	}

	// 🔊 GossipSub
	ps, err := pubsub.NewGossipSub(ctx, h)
	if err != nil {
		panic(err)
	}

	topic, err := ps.Join(TopicName)
	if err != nil {
		panic(err)
	}

	sub, err := topic.Subscribe()
	if err != nil {
		panic(err)
	}

	// 🔗 Conectar a peers manualmente (bootstrap)
	go func() {
		time.Sleep(2 * time.Second)
		connectToPeer(ctx, h, "/ip4/127.0.0.1/tcp/4002/p2p/PEER_ID_AQUI")
	}()

	// 📥 Listener de mensajes
	go func() {
		for {
			msg, err := sub.Next(ctx)
			if err != nil {
				log.Println("Error reading:", err)
				continue
			}

			var parsed ProtocolMessage
			err = json.Unmarshal(msg.Data, &parsed)
			if err != nil {
				log.Println("Invalid JSON")
				continue
			}

			// 🛡️ VALIDACIÓN CRÍTICA
			if !validateMessage(parsed) {
				log.Println("Rejected message:", parsed.MsgID)
				continue
			}

			log.Println("Accepted message:", parsed.MsgID)
		}
	}()

	// 📤 Emisor de prueba
	go func() {
		for {
			time.Sleep(5 * time.Second)
			msg := ProtocolMessage{
				MsgID:   fmt.Sprintf("%d", time.Now().UnixNano()),
				Epoch:   time.Now().Unix() / 5,
				Type:    "TASK_ANNOUNCEMENT",
				Payload: "execute something",
			}
			data, _ := json.Marshal(msg)

			err := topic.Publish(ctx, data)
			if err != nil {
				log.Println("Publish error:", err)
			} else {
				log.Println("Message sent:", msg.MsgID)
			}
		}
	}()

	select {}
}
