package tally

import (
	"encoding/xml"
	"io"
	"strings"
)

// Node is a minimal XML element tree. Tally's responses vary between
// versions and export modes (fields as attributes or children, NAME vs
// NAME.LIST, LEDGERENTRIES vs ALLLEDGERENTRIES), so the connector walks a
// generic tree instead of binding to fixed structs.
type Node struct {
	Name     string
	Attrs    map[string]string
	Text     string
	Children []*Node
}

func Parse(s string) (*Node, error) {
	dec := xml.NewDecoder(strings.NewReader(s))
	dec.Strict = false
	dec.AutoClose = xml.HTMLAutoClose
	dec.Entity = xml.HTMLEntity
	// The text is already UTF-8 (see decode); ignore any declared encoding.
	dec.CharsetReader = func(_ string, r io.Reader) (io.Reader, error) { return r, nil }
	root := &Node{Name: "#document"}
	stack := []*Node{root}
	var text strings.Builder
	for {
		tok, err := dec.Token()
		if err == io.EOF {
			break
		}
		if err != nil {
			return nil, err
		}
		switch t := tok.(type) {
		case xml.StartElement:
			n := &Node{Name: strings.ToUpper(qualified(t.Name)), Attrs: map[string]string{}}
			for _, a := range t.Attr {
				n.Attrs[strings.ToUpper(qualified(a.Name))] = a.Value
			}
			parent := stack[len(stack)-1]
			parent.Children = append(parent.Children, n)
			stack = append(stack, n)
			text.Reset()
		case xml.CharData:
			text.Write(t)
		case xml.EndElement:
			n := stack[len(stack)-1]
			if len(n.Children) == 0 {
				n.Text = strings.TrimSpace(text.String())
			}
			text.Reset()
			if len(stack) > 1 {
				stack = stack[:len(stack)-1]
			}
		}
	}
	if len(root.Children) == 1 {
		return root.Children[0], nil
	}
	return root, nil
}

func qualified(n xml.Name) string {
	// Tally uses prefixes like UDF: without declaring them consistently.
	if n.Space != "" && !strings.Contains(n.Space, "/") && n.Space != "TallyUDF" {
		return n.Space + ":" + n.Local
	}
	return n.Local
}

// Child returns the first direct child with the given (upper-case) name.
func (n *Node) Child(name string) *Node {
	if n == nil {
		return nil
	}
	for _, c := range n.Children {
		if c.Name == name {
			return c
		}
	}
	return nil
}

// ChildrenNamed returns direct children with the given name.
func (n *Node) ChildrenNamed(name string) []*Node {
	var out []*Node
	if n == nil {
		return out
	}
	for _, c := range n.Children {
		if c.Name == name {
			out = append(out, c)
		}
	}
	return out
}

func (n *Node) ChildText(name string) string {
	if c := n.Child(name); c != nil {
		return c.Text
	}
	return ""
}

// Field reads a value that Tally may emit as an attribute or a child element.
func (n *Node) Field(name string) string {
	if n == nil {
		return ""
	}
	if v, ok := n.Attrs[name]; ok && v != "" {
		return v
	}
	if c := n.Child(name); c != nil {
		if c.Text != "" {
			return c.Text
		}
		if len(c.Children) > 0 {
			return c.Children[0].Text
		}
	}
	return ""
}

// ObjectName handles the three ways Tally names an object:
// NAME="x" attribute, <NAME>x</NAME>, or <NAME.LIST><NAME>x</NAME></NAME.LIST>.
func (n *Node) ObjectName() string {
	if v := n.Field("NAME"); v != "" {
		return v
	}
	if l := n.Child("NAME.LIST"); l != nil {
		return l.ChildText("NAME")
	}
	return ""
}

// Find returns the first descendant (depth-first) with the given name.
func (n *Node) Find(name string) *Node {
	if n == nil {
		return nil
	}
	for _, c := range n.Children {
		if c.Name == name {
			return c
		}
		if f := c.Find(name); f != nil {
			return f
		}
	}
	return nil
}

// FindAll returns all descendants with the given name, not descending into
// matches (so nested objects of the same type are not double counted).
func (n *Node) FindAll(name string) []*Node {
	var out []*Node
	var walk func(*Node)
	walk = func(x *Node) {
		for _, c := range x.Children {
			if c.Name == name {
				out = append(out, c)
				continue
			}
			walk(c)
		}
	}
	if n != nil {
		if n.Name == name {
			return []*Node{n}
		}
		walk(n)
	}
	return out
}
